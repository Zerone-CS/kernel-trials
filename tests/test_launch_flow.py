"""Real CPU/HTTP acceptance tests for automatic preparation and reporting."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from engine.budget import Policy
from engine.controller import Controller
from engine.launching import Launcher
from launch import open_workspace
from service import make_server


PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "problems" / "vector_add_cpu_example"


class LaunchFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "candidate-project"
        self.project.mkdir()

    def cli(self, workspace, *arguments):
        result = subprocess.run(
            [sys.executable, str(workspace / "eval"), *arguments], cwd=workspace,
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def serve(self, controller):
        server = make_server(controller, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.addCleanup(stop)
        return "http://127.0.0.1:" + str(server.server_address[1])

    def test_automatic_workspace_to_final_then_new_attempt_and_active_resume(self):
        controller = Controller(self.root / "judge", mode="development")
        endpoint = self.serve(controller)
        launcher = Launcher(controller)
        credentials = launcher.admin_register(
            PACKAGE, owner_id="launch-flow-test", policy=Policy(max_feedback=1))
        settings = self.project / ".eval" / "launcher.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({
            "launcher_id": credentials["launcher_id"], "token": credentials["token"],
            "endpoint": endpoint,
        }))
        settings.chmod(0o600)
        self.assertFalse((self.project / "workspaces").exists())

        # No administrator-created session or predefined workspace is supplied.
        first = open_workspace(self.project, "optimize")
        workspace = Path(first["workspace"])
        self.assertTrue(workspace.is_absolute())
        self.assertTrue(workspace.is_dir())
        self.assertEqual(first["status"], "PENDING")
        self.assertEqual(first["next_action"], "run_workflow")
        self.assertEqual(first["attempt_index"], 1)
        self.assertFalse(first["reused"])
        self.assertEqual(first["budget"]["max_feedback"], 1)
        self.assertIsNone(self.cli(workspace, "status")["started_at"])

        started = self.cli(workspace, "start")
        self.assertEqual(started["state"], "OPEN")
        checked = self.cli(workspace, "check")
        self.assertEqual(checked["report"]["outcome"], "passed")
        self.assertEqual(checked["session"]["feedback_used"], 1)
        final = self.cli(workspace, "final")
        self.assertEqual(final["session"]["state"], "FINISHED")
        self.assertEqual(final["session"]["feedback_used"], 1)
        self.assertEqual(final["report"]["outcome"], "passed")
        self.assertEqual(final["report"]["correctness"]["scope"], "public_and_hidden_full")
        self.assertTrue(final["report_artifacts"]["saved"])

        report_path = workspace / "REPORT.md"
        json_path = workspace / ".eval" / "report.json"
        original_markdown, original_json = report_path.read_bytes(), json_path.read_bytes()
        report = json.loads(original_json)
        self.assertEqual(report["session"]["session_id"], first["session_id"])
        self.assertEqual(report["session"]["launcher_id"], credentials["launcher_id"])
        self.assertEqual(report["session"]["attempt_index"], 1)
        self.assertEqual(report["result"]["outcome"], "passed")
        # This CPU fixture verifies the pipeline; it has no NPU performance score.
        self.assertIsNone(report["result"]["score"])
        self.assertFalse(report["result"]["rankable"])
        self.assertIn(credentials["launcher_id"], original_markdown.decode())
        self.assertIn("| 实验轮次 | 1 |", original_markdown.decode())
        self.assertEqual(report["budget"]["feedback_used"], 1)
        self.assertEqual([op["action"] for op in self.cli(workspace, "history")["operations"]],
                         ["check", "final"])

        # A subsequent user invocation gets its own run, preserving the old report.
        second = open_workspace(self.project, "optimize")
        next_workspace = Path(second["workspace"])
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertNotEqual(next_workspace, workspace)
        self.assertEqual(second["attempt_index"], 2)
        self.assertEqual(second["status"], "PENDING")
        self.assertFalse(second["reused"])
        self.assertFalse((next_workspace / "REPORT.md").exists())
        self.assertEqual(report_path.read_bytes(), original_markdown)
        self.assertEqual(json_path.read_bytes(), original_json)

        # Resuming that active run must preserve edits and its original clock.
        next_started = self.cli(next_workspace, "start")
        kernel = next_workspace / "kernel.asc"
        edited = kernel.read_bytes() + b"\n// Candidate edit survives launcher reuse.\n"
        kernel.write_bytes(edited)
        resumed = open_workspace(self.project, "optimize")
        self.assertEqual(resumed["session_id"], second["session_id"])
        self.assertEqual(resumed["workspace"], second["workspace"])
        self.assertEqual(resumed["attempt_index"], 2)
        self.assertEqual(resumed["status"], "OPEN")
        self.assertTrue(resumed["reused"])
        self.assertEqual(kernel.read_bytes(), edited)
        current = self.cli(next_workspace, "status")
        self.assertEqual(current["started_at"], next_started["started_at"])
        self.assertEqual(current["feedback_used"], 0)
        self.assertEqual(report_path.read_bytes(), original_markdown)
        self.assertEqual(json_path.read_bytes(), original_json)
        attempts = launcher.history(credentials["launcher_id"], credentials["token"])["attempts"]
        self.assertEqual([attempt["state"] for attempt in attempts], ["FINISHED", "OPEN"])
        self.assertEqual([attempt["attempt_index"] for attempt in attempts], [1, 2])

    def test_configure_launcher_cli_mints_no_session_and_refuses_reconfiguration(self):
        # The installer only needs a script path; this test must not launch a run.
        (self.project / "launch.py").write_text("# Launcher entry placeholder for installer test.\n")
        state = self.root / "configured-judge"
        command = [
            sys.executable, str(PROJECT / "admin.py"), "configure-launcher",
            "--state", str(state), "--mode", "development", "--problem", str(PACKAGE),
            "--project-root", str(self.project), "--max-feedback", "1", "--seconds", "60",
            "--purpose", "agent_evaluation", "--endpoint", "http://127.0.0.1:8766",
        ]
        configured = subprocess.run(command, cwd=PROJECT, text=True, capture_output=True, timeout=30)
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        output = json.loads(configured.stdout)
        self.assertEqual(output["sessions_created"], 0)
        self.assertEqual(output["policy"]["max_feedback"], 1)
        self.assertEqual(output["policy"]["active_seconds"], 60)
        settings = self.project / ".eval" / "launcher.json"
        original = settings.read_bytes()
        config = json.loads(original)
        self.assertEqual(config["launcher_id"], output["launcher_id"])
        self.assertNotIn(config["token"], configured.stdout + configured.stderr)
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        for relative in (".agents/skills/op-eval/SKILL.md", ".claude/skills/op-eval/SKILL.md",
                         ".gemini/commands/op-eval.toml", ".opencode/commands/op-eval.md"):
            self.assertTrue((self.project / relative).is_file())

        controller = Controller(state, mode="development")
        launcher = Launcher(controller)
        self.assertEqual(launcher.history(config["launcher_id"], config["token"])["attempts"], [])
        with sqlite3.connect(str(controller.store.db_path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM launchers").fetchone()[0], 1)
        self.assertFalse((self.project / "workspaces").exists())

        repeated = subprocess.run(command, cwd=PROJECT, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("Launcher already configured", repeated.stderr)
        self.assertEqual(settings.read_bytes(), original)
        self.assertNotIn(config["token"], repeated.stdout + repeated.stderr)
        with sqlite3.connect(str(controller.store.db_path)) as database:
            identities = database.execute("SELECT launcher_id FROM launchers").fetchall()
            self.assertEqual(identities, [(config["launcher_id"],)])
            self.assertEqual(database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(launcher.history(config["launcher_id"], config["token"])["attempts"], [])


if __name__ == "__main__":
    unittest.main()
