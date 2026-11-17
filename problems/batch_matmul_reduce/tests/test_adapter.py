"""CPU-side migration checks; these do not claim CANN/NPU validation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from engine.problems import load_problem


ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.package = load_problem(ROOT)
        self.adapter = self.package.load_adapter()

    def known_case(self):
        # Public point 1: 32 ones dotted with 32 ones -> exactly FP32(32).
        spec = self.adapter.cases('public', 17)[0]
        directory = self.work / 'judge'
        directory.mkdir()
        files = {'x1.bin': struct.pack('<H', 0x3c00) * 32,
                 'x2.bin': struct.pack('<H', 0x3c00) * 32,
                 'golden.bin': struct.pack('<f', 32.0)}
        artifacts = {}
        for name, raw in files.items():
            (directory / name).write_bytes(raw)
            artifacts[name[:-4]] = {'file': name, 'bytes': len(raw),
                                    'sha256': hashlib.sha256(raw).hexdigest()}
        manifest = {'spec': {k: v for k, v in spec.items() if k not in ('suite', 'performance')},
                    'generator_sha256': hashlib.sha256((ROOT / 'trusted/cases.py').read_bytes()).hexdigest(),
                    'artifacts': artifacts}
        (directory / 'case.json').write_text(json.dumps(manifest))
        return spec, directory

    def test_catalog_resampling_and_preserved_reference(self):
        public = self.adapter.cases('public', 17)
        hidden = self.adapter.cases('hidden', 17)
        self.assertEqual(len(public), 15)
        self.assertEqual(len(hidden), 24)
        self.assertEqual(len({s['case_id'] for s in public + hidden}), 39)
        self.assertTrue(all(s['performance'] for s in public))
        self.assertTrue(all(not s['performance'] for s in hidden))
        self.assertNotEqual([s['seed'] for s in hidden],
                            [s['seed'] for s in self.adapter.cases('hidden', 18)])
        self.assertEqual(public, self.adapter.cases('public', 17))
        self.assertEqual(hashlib.sha256(self.package.path('reference').read_bytes()).hexdigest(),
                         '6368f40c1597e330c8c5f44d030874da54f01e613760b87f53e06f3f6ffabc7e')
        protocol = json.loads(self.package.path('protocol').read_text())
        self.assertTrue(protocol['score']['enabled'])
        self.assertTrue(protocol['timing']['supported_by_v2_runner'])
        self.assertEqual(protocol['timing']['metric'], 'msprof_device_task_duration')
        self.package.assert_unchanged()

    def test_adapter_compares_independent_bytes_and_all_captures(self):
        spec, directory = self.known_case()
        output = self.work / 'output.bin'
        output.write_bytes(struct.pack('<f', 32))
        capture = self.work / 'output.bin.call-0'
        capture.write_bytes(struct.pack('<f', 32))
        result = self.adapter.verify(spec, directory, output, expected_calls=1)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_captures'], 2)
        capture.write_bytes(struct.pack('<f', 0))
        self.assertFalse(self.adapter.verify(spec, directory, output, expected_calls=1)['passed'])
        capture.unlink()
        self.assertEqual(self.adapter.verify(spec, directory, output, expected_calls=1)['status'],
                         'CAPTURE_COUNT_MISMATCH')
        output.write_bytes(struct.pack('<f', float('nan')))
        self.assertEqual(self.adapter.verify(spec, directory, output)['status'], 'OUTPUT_NONFINITE')
        (directory / 'golden.bin').write_bytes(struct.pack('<f', 0))
        with self.assertRaisesRegex(ValueError, 'artifact identity mismatch'):
            self.adapter.verify(spec, directory, output)

    def test_fixed_build_and_runner_argv_no_oracle_export(self):
        spec = self.adapter.cases('public', 17)[1]
        project = self.work / 'project'
        self.package.stage_project(self.package.starter_path('optimize').read_bytes(), project)
        self.assertEqual({p.name for p in project.iterdir()}, {'main.asc', 'CMakeLists.txt', 'kernel.asc'})
        commands = self.package.build_commands(project, self.work / 'build')
        self.assertEqual(commands[0][0], 'cmake')
        args = self.adapter.runner_args(spec, self.work / 'execution', self.work / 'output',
                                       mode='correctness', repeat=3, warmup=0)
        self.assertNotIn('golden.bin', ' '.join(args))
        self.assertIn('--transpose-x1', args)
        self.assertEqual(args[args.index('--repeat') + 1], '3')
        self.package.assert_unchanged()

    @unittest.skipUnless(importlib.util.find_spec('numpy') and importlib.util.find_spec('ml_dtypes'),
                         'NumPy/ml_dtypes are not installed; no installation requested')
    def test_actual_generator_on_small_public_and_hidden_cases(self):
        for spec in [self.adapter.cases('public', 1)[0], self.adapter.cases('hidden', 1)[0]]:
            directory = self.work / spec['case_id']
            manifest = self.adapter.prepare(spec, directory)
            self.assertEqual(manifest['inputs'], ['x1.bin', 'x2.bin'])
            self.assertIn('golden.bin', manifest['oracle_files'])
            output = self.work / (spec['case_id'] + '.bin')
            output.write_bytes((directory / 'golden.bin').read_bytes())
            self.assertTrue(self.adapter.verify(spec, directory, output)['passed'])


if __name__ == '__main__':
    unittest.main()
