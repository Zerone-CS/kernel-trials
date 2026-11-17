import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import assigned


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mapping = {"from_scratch": "workspaces/scratch", "optimize": "workspaces/optimization"}
        self.sessions = {}
        for index, track in enumerate(self.mapping, 1):
            path = self.root / self.mapping[track] / ".eval"
            path.mkdir(parents=True)
            value = {"session_id": str(index) * 32, "token": "private-token-" + track,
                     "endpoint": "http://127.0.0.1:8765"}
            (path / "session.json").write_text(json.dumps(value))
            self.sessions[track] = value

    def status(self, config):
        track = next(track for track, value in self.sessions.items() if value["session_id"] == config["session_id"])
        return {"session_id": config["session_id"], "track": track, "state": "PENDING"}

    def install(self):
        return assigned.install_assignment_routes(self.root, self.mapping)

    def test_install_routes_existing_sessions_without_credentials_or_network(self):
        with patch.object(assigned, "_session_status") as request:
            result = self.install()
        request.assert_not_called()
        self.assertEqual(set(result["assignments"]), {"from_scratch", "optimize"})
        contents = (self.root / ".eval/assignments.json").read_text()
        self.assertNotIn("private-token", contents)
        for name in (".agents/skills/op-eval/SKILL.md", ".claude/skills/op-eval/SKILL.md",
                     ".gemini/commands/op-eval.toml", ".opencode/commands/op-eval.md"):
            self.assertTrue((self.root / name).is_file())
        before = {track: (self.root / relative / ".eval/session.json").read_bytes() for track, relative in self.mapping.items()}
        with patch.object(assigned, "_session_status", side_effect=self.status) as request:
            result = assigned.resolve_assignment(self.root)
        self.assertEqual(result, {"workspace": str(self.root / self.mapping["from_scratch"]), "track": "from_scratch"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(before, {track: (self.root / relative / ".eval/session.json").read_bytes() for track, relative in self.mapping.items()})

    def test_cli_selects_default_or_optimize_and_only_outputs_path_and_track(self):
        self.install()
        for argv, track in (([], "from_scratch"), (["optimize"], "optimize")):
            output = io.StringIO()
            with patch.object(assigned, "__file__", str(self.root / "assigned.py")), patch.object(assigned, "_session_status", side_effect=self.status), contextlib.redirect_stdout(output):
                result = assigned.main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), {"workspace": str(self.root / self.mapping[track]), "track": track})

    def test_terminal_session_requires_admin_and_cannot_reset(self):
        self.install()
        for state in ("FINISHED", "EXPIRED", "CANCELLED"):
            status = {"session_id": "1" * 32, "track": "from_scratch", "state": state}
            with self.subTest(state=state), patch.object(assigned, "_session_status", return_value=status) as request, self.assertRaises(assigned.AssignmentError) as raised:
                assigned.resolve_assignment(self.root)
            self.assertEqual(raised.exception.code, "ASSIGNMENT_CLOSED")
            self.assertIn("administrator", str(raised.exception))
            self.assertEqual(request.call_count, 1)
        status = {"session_id": "1" * 32, "track": "from_scratch", "state": "FINALIZING"}
        with patch.object(assigned, "_session_status", return_value=status), self.assertRaises(assigned.AssignmentError) as raised:
            assigned.resolve_assignment(self.root)
        self.assertEqual(raised.exception.code, "ASSIGNMENT_FINALIZING")
        self.assertIn("wait", str(raised.exception))

    def test_user_edited_or_untracked_root_command_blocks_all_writes(self):
        target = self.root / ".claude/skills/op-eval/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("my custom entry")
        with self.assertRaisesRegex(assigned.AssignmentError, "Preserved"):
            self.install()
        self.assertEqual(target.read_text(), "my custom entry")
        self.assertFalse((self.root / ".eval/assignments.json").exists())
        target.unlink()
        self.install()
        original_map = (self.root / ".eval/assignments.json").read_bytes()
        target.write_text("edited managed entry")
        with self.assertRaisesRegex(assigned.AssignmentError, "Preserved"):
            self.install()
        self.assertEqual(target.read_text(), "edited managed entry")
        self.assertEqual((self.root / ".eval/assignments.json").read_bytes(), original_map)

    def test_managed_reassignment_updates_only_unchanged_installer_files(self):
        self.install()
        (self.root / "user_notes.txt").write_text("keep")
        replacement = self.root / "workspaces/new_scratch/.eval"
        replacement.mkdir(parents=True)
        (replacement / "session.json").write_text(json.dumps(dict(self.sessions["from_scratch"], session_id="3" * 32)))
        mapping = dict(self.mapping, from_scratch="workspaces/new_scratch")
        assigned.install_assignment_routes(self.root, mapping)
        saved = json.loads((self.root / ".eval/assignments.json").read_text())
        self.assertEqual(saved["assignments"]["from_scratch"]["session_id"], "3" * 32)
        self.assertEqual((self.root / "user_notes.txt").read_text(), "keep")

    def test_missing_duplicate_or_outside_workspace_fails_before_install(self):
        for mapping in ({"from_scratch": "missing", "optimize": self.mapping["optimize"]},
                        dict(self.mapping, optimize=self.mapping["from_scratch"]),
                        dict(self.mapping, from_scratch="../outside")):
            with self.subTest(mapping=mapping), self.assertRaises(assigned.AssignmentError):
                assigned.install_assignment_routes(self.root, mapping)
        self.assertFalse((self.root / ".eval/assignments.json").exists())

    def test_symlinked_installer_path_and_workspace_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".eval").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(assigned.AssignmentError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / ".eval").unlink()
        (self.root / "workspace_alias").symlink_to(self.root / self.mapping["from_scratch"], target_is_directory=True)
        with self.assertRaises(assigned.AssignmentError):
            assigned.install_assignment_routes(self.root, dict(self.mapping, from_scratch="workspace_alias"))

    def test_assignment_or_workspace_session_tamper_does_not_route_elsewhere(self):
        self.install()
        path = self.root / ".eval/assignments.json"
        original = path.read_bytes()
        value = json.loads(original)
        value["assignments"]["from_scratch"]["workspace"] = self.mapping["optimize"]
        path.write_text(json.dumps(value))
        with patch.object(assigned, "_session_status") as request, self.assertRaises(assigned.AssignmentError):
            assigned.resolve_assignment(self.root)
        request.assert_not_called()
        path.write_bytes(original)
        session_path = self.root / self.mapping["from_scratch"] / ".eval/session.json"
        session_path.write_text(json.dumps(self.sessions["optimize"]))
        with patch.object(assigned, "_session_status") as request, self.assertRaises(assigned.AssignmentError):
            assigned.resolve_assignment(self.root)
        request.assert_not_called()
