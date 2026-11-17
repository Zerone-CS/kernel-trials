"""Static documentation checks on isolated fixtures, without Agent execution."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_docs.py'
SPEC = importlib.util.spec_from_file_location('op_eval_check_docs', SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'docs').mkdir()
        for name in checker.ENTRIES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('prompt = "Run evaluation"\n' if path.suffix == '.toml' else '# Entry\n')

    def check(self):
        return checker.check_repository(self.root, validate_experiments=False)

    def test_valid_links_json_fences_and_template_exclusions(self):
        (self.root / 'docs/a file.md').write_text('# Target\n')
        (self.root / 'README.md').write_text('''# Sample
[space](<docs/a file.md#unknown-anchor>)
[encoded](docs/a%20file.md)
[web](https://example.invalid/nonexistent)
[anchor](#unverified)
[template](docs/<TASK_ID>/README.md)
[bare template](<TASK_ID>)
[variable](${PROJECT}/README.md)
[reference][guide]
[guide]: <docs/a file.md> "Guide"
```json
{"a": [1, true, null]}
```
```text
[example](not-a-real-link.md)
```
''')
        # Private site-specific historical records are outside the public scan.
        (self.root / 'SERVER_VALIDATION.md').write_text('[private](missing.md)\n```')
        self.assertEqual(self.check()['errors'], [])

    def test_missing_link_is_reported_relative_to_document(self):
        (self.root / 'docs/guide.md').write_text('[missing](../gone.md#section)\n')
        errors = self.check()['errors']
        self.assertEqual(len(errors), 1)
        self.assertIn('docs/guide.md:1:', errors[0])
        self.assertIn('missing local link target: ../gone.md#section', errors[0])

    def test_invalid_json_and_unclosed_fence_are_independent_errors(self):
        (self.root / 'README.md').write_text('```json\n{"a":}\n```\n\n~~~text\nunclosed\n')
        errors = self.check()['errors']
        self.assertEqual(len(errors), 2)
        self.assertTrue(any('invalid JSON code block' in e for e in errors))
        self.assertTrue(any('unclosed code fence' in e for e in errors))

    def test_gemini_prompt_markdown_is_decoded(self):
        target = self.root / '.gemini/commands/op-eval.toml'
        target.write_text('prompt = ' + json.dumps('```json\n{"bad":}\n```\n') + '\n')
        errors = self.check()['errors']
        self.assertEqual(len(errors), 1)
        self.assertIn('.gemini/commands/op-eval.toml:', errors[0])
        self.assertIn('invalid JSON', errors[0])

    def test_missing_entry_is_reported(self):
        (self.root / checker.ENTRIES[0]).unlink()
        self.assertIn(checker.ENTRIES[0] + ': managed Agent entry is missing', self.check()['errors'])


if __name__ == '__main__':
    unittest.main()
