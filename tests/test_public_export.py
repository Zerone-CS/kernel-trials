import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.export_public import (DIRECTORIES, ENTRY_FILES, ROOT_FILES, SCRIPT_FILES,
                                   ExportError, export_public)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / 'source'
        self.output = self.base / 'public'
        self.source.mkdir()
        for name in ROOT_FILES + ENTRY_FILES + SCRIPT_FILES:
            self.write(name, ('public fixture: ' + name + '\n').encode())
        for name in DIRECTORIES:
            (self.source / name).mkdir(exist_ok=True)
        self.write('engine/example.py', b'VALUE = 1\n')
        self.write('docs/guide.md', b'# Guide\n')
        self.write('experiments/example.json', b'{"conditions": []}\n')
        self.write('problems/example/problem.json', b'{"schema_version": 1}\n')
        self.write('problems/example/kernel.asc', b'// exact fixture bytes\r\n')

    def write(self, name, data=b'private fixture'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_allowlist_excludes_credentials_runtime_and_unknown_roots(self):
        excluded = ('.eval/launcher.json', '.evals/session.json', 'workspaces/run/kernel.asc',
                    'evidence/summary.json', 'SERVER_VALIDATION.md', 'private-notes.md',
                    'archive.zip', 'task.pdf', '.claude/settings.json',
                    'engine/__pycache__/example.pyc', 'engine/runtime.log', 'engine/binary.so',
                    'docs/image.png', 'tests/.eval/session.json', 'experiments/.env',
                    'experiments/settings.local.json', 'experiments/method.local.json',
                    'experiments/private/notes.md', 'tests/state/binding.json')
        for name in excluded:
            self.write(name)
        manifest = export_public(self.source, self.output)
        selected = {item['path'] for item in manifest['files']}
        for name in excluded:
            self.assertNotIn(name, selected)
            self.assertFalse((self.output / name).exists())
        self.assertIn('engine/example.py', selected)
        self.assertIn('.claude/skills/op-eval/SKILL.md', selected)
        self.assertFalse((self.output / '.git').exists())

    def test_manifest_matches_bytes_and_preserves_problem_identity(self):
        manifest = export_public(self.source, self.output)
        self.assertEqual(manifest, json.loads((self.output / 'PUBLIC_MANIFEST.json').read_text()))
        self.assertNotIn(str(self.source), json.dumps(manifest))
        self.assertFalse(manifest['license']['present'])
        self.assertFalse(manifest['license']['selected_by_exporter'])
        self.assertIn('project_license_decision', manifest['review_required'])
        for item in manifest['files']:
            data = (self.output / item['path']).read_bytes()
            self.assertEqual(data, (self.source / item['path']).read_bytes())
            self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(item['size_bytes'], len(data))
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (self.source / 'problems/example').iterdir()}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        self.assertEqual(manifest['problem_packages'],
                         [{'path':'problems/example','file_count':2,'package_digest':digest}])

    def test_existing_output_and_inside_source_are_rejected_without_changes(self):
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_text('keep')
        with self.assertRaisesRegex(ExportError, 'refusing overwrite'):
            export_public(self.source, self.output)
        self.assertEqual(sentinel.read_text(), 'keep')
        with self.assertRaisesRegex(ExportError, 'outside the source tree'):
            export_public(self.source, self.source / 'new-export')
        self.assertFalse((self.source / 'new-export').exists())

    def test_selected_file_and_directory_symlinks_are_rejected(self):
        target = self.base / 'private.txt'
        target.write_text('do not export')
        linked = self.source / 'docs/linked.md'
        linked.symlink_to(target)
        with self.assertRaisesRegex(ExportError, 'symlink'):
            export_public(self.source, self.output)
        self.assertFalse(self.output.exists())
        linked.unlink()
        (self.source / 'docs/linked').symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ExportError, 'symlink'):
            export_public(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_required_source_missing_fails_before_creating_output(self):
        (self.source / 'README.md').unlink()
        with self.assertRaisesRegex(ExportError, 'Required public source is missing: README.md'):
            export_public(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_filtering_a_problem_file_is_refused_without_cleaning_it(self):
        private = self.write('problems/example/raw.log', b'unchanged\n')
        with self.assertRaisesRegex(ExportError, 'Cannot preserve problem package identity'):
            export_public(self.source, self.output)
        self.assertEqual(private.read_bytes(), b'unchanged\n')
        self.assertFalse(self.output.exists())

    def test_optional_license_is_copied_without_selecting_it(self):
        self.write('LICENSE', b'Author-supplied license text\n')
        self.write('NOTICE', b'Author-supplied attribution\n')
        manifest = export_public(self.source, self.output)
        self.assertTrue(manifest['license']['present'])
        self.assertEqual(manifest['license']['files'], ['LICENSE'])
        self.assertEqual(manifest['license']['notice_files'], ['NOTICE'])
        self.assertFalse(manifest['license']['selected_by_exporter'])

        self.assertIn('project_license_review', manifest['review_required'])
        self.assertNotIn('project_license_decision', manifest['review_required'])

    def test_dry_run_checks_manifest_but_creates_nothing(self):
        manifest = export_public(self.source, self.output, dry_run=True)
        self.assertGreater(len(manifest['files']), 0)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
