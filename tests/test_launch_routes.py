import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from assigned import install_assignment_routes
from launch_routes import install_launch_routes, LaunchRoutesError


ENTRIES = (".agents/skills/op-eval/SKILL.md", ".claude/skills/op-eval/SKILL.md",
           ".gemini/commands/op-eval.toml", ".opencode/commands/op-eval.md")


class LaunchRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # If an installer incorrectly executes it, it leaves this observable
        # marker. Route installation must be file-only, without session setup.
        (self.root / "launch.py").write_text("from pathlib import Path\nPath('executed').write_text('wrong')\n")

    def install_legacy(self):
        mapping = {}
        for index, track in enumerate(("from_scratch", "optimize"), 1):
            relative = "workspaces/" + track
            directory = self.root / relative / ".eval"
            directory.mkdir(parents=True)
            (directory / "session.json").write_text(json.dumps({"session_id": str(index) * 32,
                                                                 "token": "private", "endpoint": "http://127.0.0.1:1"}))
            mapping[track] = relative
        install_assignment_routes(self.root, mapping)

    def test_installs_all_entrypoints_without_executing_launcher(self):
        result = install_launch_routes(self.root)
        self.assertEqual(result["files_changed"], 4)
        self.assertFalse((self.root / "executed").exists())
        for relative in ENTRIES:
            content = (self.root / relative).read_text()
            self.assertIn("python3 launch.py", content)
            self.assertIn("OP_EVAL_WORKFLOW.md", content)
            self.assertIn("attempt_index", content)
            self.assertIn("pending_operation_id", content)
            self.assertIn("./eval report", content)
            self.assertIn("persisted request_id", content)
        manifest = json.loads((self.root / ".eval/launch_routes.manifest.json").read_text())
        self.assertEqual(set(manifest["files"]), set(ENTRIES))
        for relative, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.root / relative).read_bytes()).hexdigest(), digest)

    def test_outer_and_v2_entrypoints_reference_the_same_script_without_copying_state(self):
        v2 = self.root / "op_eval_v2"
        v2.mkdir()
        (v2 / "launch.py").write_bytes((self.root / "launch.py").read_bytes())
        outer = install_launch_routes(self.root, "op_eval_v2/launch.py")
        inner = install_launch_routes(v2)
        self.assertEqual(outer["script"], inner["script"])
        self.assertIn("python3 op_eval_v2/launch.py", (self.root / ENTRIES[0]).read_text())
        self.assertIn("python3 launch.py", (v2 / ENTRIES[0]).read_text())
        self.assertFalse((self.root / ".eval/assignments.json").exists())
        self.assertFalse((v2 / ".eval/assignments.json").exists())

    def test_unchanged_legacy_entries_can_be_adopted_without_changing_assignment_data(self):
        self.install_legacy()
        old_map = (self.root / ".eval/assignments.json").read_bytes()
        old_manifest = (self.root / ".eval/assignment_routes.manifest.json").read_bytes()
        result = install_launch_routes(self.root)
        self.assertEqual(set(result["adopted_assignment_entries"]), set(ENTRIES))
        self.assertEqual((self.root / ".eval/assignments.json").read_bytes(), old_map)
        self.assertEqual((self.root / ".eval/assignment_routes.manifest.json").read_bytes(), old_manifest)
        self.assertNotIn("assigned.py", (self.root / ENTRIES[0]).read_text())

    def test_legacy_user_edit_blocks_entire_install(self):
        self.install_legacy()
        target = self.root / ENTRIES[2]
        target.write_text("user custom command")
        before = {relative: (self.root / relative).read_bytes() for relative in ENTRIES}
        with self.assertRaisesRegex(LaunchRoutesError, "Preserved"):
            install_launch_routes(self.root)
        self.assertEqual({relative: (self.root / relative).read_bytes() for relative in ENTRIES}, before)
        self.assertFalse((self.root / ".eval/launch_routes.manifest.json").exists())

    def test_new_owner_hash_wins_over_legacy_hash_after_takeover(self):
        self.install_legacy()
        old_entry = (self.root / ENTRIES[0]).read_bytes()
        install_launch_routes(self.root)
        (self.root / ENTRIES[0]).write_bytes(old_entry)
        with self.assertRaisesRegex(LaunchRoutesError, "Preserved"):
            install_launch_routes(self.root)
        self.assertEqual((self.root / ENTRIES[0]).read_bytes(), old_entry)

    def test_idempotence_and_script_route_update_preserve_user_files(self):
        install_launch_routes(self.root)
        self.assertEqual(install_launch_routes(self.root)["files_changed"], 0)
        (self.root / "notes.txt").write_text("keep")
        scripts = self.root / "other"
        scripts.mkdir()
        (scripts / "launch.py").write_text("# script")
        self.assertEqual(install_launch_routes(self.root, "other/launch.py")["files_changed"], 4)
        self.assertEqual((self.root / "notes.txt").read_text(), "keep")

    def test_untracked_file_symlink_and_escape_are_rejected(self):
        for relative in ("../launch.py", "/tmp/launch.py", "missing.py"):
            with self.subTest(relative=relative), self.assertRaises(LaunchRoutesError):
                install_launch_routes(self.root, relative)
        target = self.root / ENTRIES[1]
        target.parent.mkdir(parents=True)
        target.write_text("untracked custom skill")
        with self.assertRaisesRegex(LaunchRoutesError, "Preserved"):
            install_launch_routes(self.root)
        target.unlink()
        outside = self.root / "outside.md"
        outside.write_text("keep")
        target.symlink_to(outside)
        with self.assertRaises(LaunchRoutesError):
            install_launch_routes(self.root)
        self.assertEqual(outside.read_text(), "keep")

    def test_entry_routes_distinguish_pending_final_and_finished_without_new_budget(self):
        install_launch_routes(self.root)
        content = (self.root / ENTRIES[0]).read_text()
        for required in ("PENDING or OPEN", "next_action=run_workflow", "next_action=resume_operation",
                         "FINALIZING", "FINISHED or EXPIRED", "next_action=read_report",
                         "do not call launch again during this", "later, new USER invocation"):
            self.assertIn(required, content)

    def test_all_agents_receive_task_listing_single_trial_and_comparison_modes(self):
        install_launch_routes(self.root)
        # Check the effective instruction payload, including Gemini's escaped
        # TOML string, rather than just the installer template or its labels.
        payloads = {}
        for relative in ENTRIES:
            payload = (self.root / relative).read_text()
            if relative.endswith(".toml"):
                payload = json.loads(payload.split("prompt = ", 1)[1])
            payloads[relative] = payload
            for required in ("python3 launch.py case06 optimize", "python3 launch.py tasks",
                             "python3 launch.py compare CONFIG", "selecting full and from_scratch",
                             "Listing tasks does not allocate", "fresh Agent process for each trial",
                             "current conversation must not implement the trial",
                             "resumes the existing experiment", "--new as an error recovery",
                             "relative comparison configuration path", "Codex invokes $op-eval",
                             "compare CONFIG` ONCE", "driver already emits progress",
                             "SAME command's execution handle", "Do not\n  start a separate Monitor",
                             "persistent `tail -f`/`tail -F`", "Do not rerun the command to poll progress",
                             "result JSON", "without starting any additional run",
                             "returns a task/job handle", "wait/result mechanism on the SAME handle",
                             "Reading a progress/output file once is\n  NOT completion",
                             "Do not send a final assistant response while the driver or",
                             "all\n  configured trials terminal", "explicit driver\n  failure"):
                with self.subTest(entry=relative, requirement=required):
                    self.assertIn(required, payload)
        self.assertIn("Requested arguments: $ARGUMENTS", payloads[ENTRIES[1]])
        self.assertIn("Requested arguments: {{args}}", payloads[ENTRIES[2]])
        self.assertIn("Requested arguments: $ARGUMENTS", payloads[ENTRIES[3]])
        self.assertFalse((self.root / "executed").exists())
