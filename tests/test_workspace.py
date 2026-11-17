import json
from pathlib import Path
import shutil
import tempfile
import unittest

from engine.controller import Controller
from workspace import export_workspace, ENTRY, WORKFLOW


PACKAGE = Path(__file__).resolve().parents[1] / "problems" / "vector_add_cpu_example"


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = Controller(self.root / "judge")

    def create(self, package=PACKAGE):
        return self.controller.admin_create(package, track="optimize", owner_id="admin")

    def test_export_is_pending_public_only_and_all_commands_are_discoverable(self):
        credentials = self.create()
        root = export_workspace(self.controller, credentials, self.root / "candidate", "http://127.0.0.1:8765")
        for path in (".agents/skills/op-eval/SKILL.md", ".claude/skills/op-eval/SKILL.md",
                     ".gemini/commands/op-eval.toml", ".opencode/commands/op-eval.md"):
            self.assertTrue((root / path).is_file())
        self.assertEqual((root / "kernel.asc").read_bytes(), (PACKAGE / "starters/optimize/kernel.asc").read_bytes())
        self.assertFalse((root / "judge.py").exists())
        self.assertFalse((root / "reference").exists())
        status = self.controller.store.status(credentials["session_id"], credentials["token"])
        self.assertEqual(status["state"], "PENDING")
        self.assertIsNone(status["started_at"])
        config = json.loads((root / ".eval/session.json").read_text())
        self.assertEqual(config["session_id"], credentials["session_id"])
        self.assertEqual((root / ".eval/session.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue((root / "eval").stat().st_mode & 0o111)

    def test_existing_destination_and_symlink_never_overwritten(self):
        credentials = self.create()
        directory = self.root / "existing"
        directory.mkdir()
        (directory / "user.txt").write_text("keep")
        with self.assertRaises(ValueError):
            export_workspace(self.controller, credentials, directory, "http://127.0.0.1:1")
        self.assertEqual((directory / "user.txt").read_text(), "keep")
        link = self.root / "link"
        link.symlink_to(self.root / "absent")
        with self.assertRaises(ValueError):
            export_workspace(self.controller, credentials, link, "http://127.0.0.1:1")

    def test_package_cannot_override_client_file(self):
        package = self.root / "collision"
        shutil.copytree(PACKAGE, package)
        (package / "harness/eval").write_text("override-client")
        credentials = self.create(package)
        destination = self.root / "candidate"
        with self.assertRaisesRegex(ValueError, "reserved"):
            export_workspace(self.controller, credentials, destination, "http://127.0.0.1:1")
        self.assertFalse(destination.exists())

    def test_bare_entry_routes_admin_smoke_purpose_after_start_and_explains_fallback(self):
        credentials = self.controller.admin_create(PACKAGE, track="optimize", owner_id="admin",
                                                   evaluation_purpose="entry_smoke")
        root = export_workspace(self.controller, credentials, self.root / "smoke", "http://127.0.0.1:1")
        started = self.controller.start(credentials["session_id"], credentials["token"])
        self.assertEqual(started["claimed_agent_metadata"]["evaluation_purpose"], "entry_smoke")
        # Discovery prompts route the bare command through returned server
        # purpose, rather than assuming an empty argument always means editing.
        for name in (".agents/skills/op-eval/SKILL.md", ".claude/skills/op-eval/SKILL.md",
                     ".gemini/commands/op-eval.toml", ".opencode/commands/op-eval.md"):
            prompt = (root / name).read_text()
            self.assertIn("claimed_agent_metadata.evaluation_purpose", prompt)
            self.assertIn("entry_smoke", prompt)
            self.assertIn("no kernel edits", prompt)
        self.assertIn("last 60 seconds", WORKFLOW)
        self.assertIn("same final-once gate", WORKFLOW)
        self.assertIn("Without any submitted source", WORKFLOW)
