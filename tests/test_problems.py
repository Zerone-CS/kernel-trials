"""Package identity, fixed-file staging and an independently judged CPU fixture."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from engine.problems import (
    ProblemError, discover_problems, load_problem, validate_candidate_source,
)


EXAMPLE = Path(__file__).resolve().parents[1] / 'problems' / 'vector_add_cpu_example'


class ProblemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='package tests ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package_dir = self.root / 'registry' / 'vector_add'
        shutil.copytree(EXAMPLE, self.package_dir)

    def change_manifest(self, change):
        path = self.package_dir / 'problem.json'
        manifest = json.loads(path.read_text())
        change(manifest)
        path.write_text(json.dumps(manifest))

    def test_load_discovery_and_expected_identity(self):
        package = load_problem(self.package_dir)
        self.assertEqual(package.problem_id, 'vector_add_cpu_example')
        self.assertEqual(len(package.digest), 64)
        self.assertEqual(discover_problems(self.root / 'registry')[0].digest, package.digest)
        self.assertEqual(load_problem(self.package_dir, package.digest).digest, package.digest)
        with self.assertRaises(ProblemError):
            load_problem(self.package_dir, '0' * 64)

    def test_digest_covers_hidden_files_references_and_relative_paths(self):
        package = load_problem(self.package_dir)
        original = package.digest
        private = self.package_dir / '.private'
        private.write_bytes(b'private data')
        self.assertNotEqual(load_problem(self.package_dir).digest, original)
        with self.assertRaises(ProblemError):
            package.assert_unchanged()
        private.unlink()
        self.assertEqual(load_problem(self.package_dir).digest, original)
        reference = self.package_dir / 'reference' / 'kernel.asc'
        reference.write_bytes(reference.read_bytes() + b'\n// changed\n')
        self.assertNotEqual(load_problem(self.package_dir).digest, original)
        package = load_problem(self.package_dir)
        extra = self.package_dir / 'one.txt'
        extra.write_bytes(b'same content')
        digest = load_problem(self.package_dir).digest
        extra.rename(self.package_dir / 'two.txt')
        self.assertNotEqual(load_problem(self.package_dir).digest, digest)

    def test_package_symlinks_and_special_files_fail_closed(self):
        link = self.package_dir / 'external'
        link.symlink_to(self.root)
        with self.assertRaisesRegex(ProblemError, 'Symlink'):
            load_problem(self.package_dir)
        link.unlink()
        link.symlink_to('nonexistent')
        with self.assertRaisesRegex(ProblemError, 'Symlink'):
            load_problem(self.package_dir)
        link.unlink()
        root_link = self.root / 'root_link'
        root_link.symlink_to(self.package_dir)
        with self.assertRaises(ProblemError):
            load_problem(root_link)

    def test_manifest_paths_cannot_escape_or_alias(self):
        for value in ('../task.md', '/etc/passwd', './task.md', 'public//header.h',
                      'public\\header.h', 'public/../task.md'):
            with self.subTest(value=value):
                self.change_manifest(lambda m: m.update(task=value))
                with self.assertRaises(ProblemError):
                    load_problem(self.package_dir)

    def test_missing_test_protocol_or_oracle_adapter_is_an_error(self):
        for name in ('cases.json', 'protocol.json', 'judge.py'):
            path = self.package_dir / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaises(ProblemError):
                load_problem(self.package_dir)
            path.write_bytes(original)
        (self.package_dir / 'judge.py').write_text('def cases(suite, seed): return []\n')
        with self.assertRaisesRegex(ProblemError, 'prepare'):
            load_problem(self.package_dir).load_adapter()

    def test_public_exports_cannot_replace_engineering_or_private_files(self):
        for exports in ({'kernel.asc': 'public/operator_contract.h'},
                        {'kernel.asc/child': 'public/operator_contract.h'},
                        {'main.cpp': 'public/operator_contract.h'},
                        {'main.cpp/child': 'public/operator_contract.h'},
                        {'judge.py': 'judge.py'},
                        {'reference.asc': 'reference/kernel.asc'},
                        {'a': 'public/operator_contract.h', 'a/b': 'public/operator_contract.h'}):
            with self.subTest(exports=exports):
                self.change_manifest(lambda m: m.update(public_files=exports))
                with self.assertRaises(ProblemError):
                    load_problem(self.package_dir)

    def test_build_is_argv_with_only_explicit_placeholders(self):
        package = load_problem(self.package_dir)
        project = self.root / 'project with spaces'
        build = self.root / 'build with spaces'
        command = package.build_commands(project, build)[0]
        self.assertIn(str(project.resolve() / 'main.cpp'), command)
        self.assertIn(str(build.resolve() / 'runner'), command)
        for commands in ('c++ main.cpp', [['c++', '{project.__class__}']],
                         [['c++', '{unknown}']], [['c++', '{project!r}']]):
            self.change_manifest(lambda m: m['build'].update(commands=commands))
            with self.assertRaises(ProblemError):
                load_problem(self.package_dir)

    def test_source_gate_rejects_external_and_macro_includes(self):
        policy = load_problem(self.package_dir).manifest['candidate_policy']
        valid = b'// #include "/private"\n/* #include <other> */\n#include "operator_contract.h"\n'
        result = validate_candidate_source(valid, policy)
        self.assertEqual(result['include_headers'], ['operator_contract.h'])
        self.assertFalse(result['security_boundary'])
        for source in (b'#include "/etc/passwd"', b'#include "../x.h"',
                       b'#include <fstream>', b'#define H "x"\n#include H',
                       b'#include_next <cstddef>', b'#import <cstddef>',
                       b'#inc\\\nlude "/etc/passwd"', b'\x00', b''):
            with self.subTest(source=source):
                with self.assertRaises(ProblemError):
                    validate_candidate_source(source, policy)

    def test_stage_exports_only_fixed_project_and_candidate_snapshot(self):
        package = load_problem(self.package_dir)
        source = package.starter_path('optimize').read_bytes()
        project = self.root / 'project'
        identity = package.stage_project(source, project)
        self.assertEqual((project / 'kernel.asc').read_bytes(), source)
        self.assertEqual(set(p.name for p in project.iterdir()),
                         {'main.cpp', 'operator_contract.h', 'kernel.asc'})
        self.assertEqual(identity['problem_digest'], package.digest)
        with self.assertRaises(ProblemError):
            package.stage_project(source, project)
        package.starter_path('optimize').write_bytes(source + b'\n// edit\n')
        self.assertEqual((project / 'kernel.asc').read_bytes(), source)
        with self.assertRaises(ProblemError):
            package.stage_project(source, self.root / 'another')

    def test_loading_adapter_does_not_mutate_package_or_write_bytecode(self):
        package = load_problem(self.package_dir)
        package.load_adapter()
        package.assert_unchanged()
        self.assertFalse(list(self.package_dir.rglob('__pycache__')))

    def test_duplicate_registry_identity_is_rejected(self):
        shutil.copytree(self.package_dir, self.root / 'registry' / 'duplicate')
        with self.assertRaisesRegex(ProblemError, 'Duplicate'):
            discover_problems(self.root / 'registry')

    def test_adapter_seeded_data_and_own_oracle(self):
        adapter = load_problem(self.package_dir).load_adapter()
        first = adapter.cases('hidden', 42)[0]
        self.assertEqual(first, adapter.cases('hidden', 42)[0])
        other = adapter.cases('hidden', 43)[0]
        one, two = self.root / 'one', self.root / 'two'
        manifest = adapter.prepare(first, one)
        self.assertEqual(manifest['inputs'], ['input.bin'])
        self.assertEqual(manifest['oracle_files'], ['golden.bin'])
        self.assertEqual(adapter.prepare(first, one), manifest)
        adapter.prepare(other, two)
        self.assertNotEqual((one / 'input.bin').read_bytes(), (two / 'input.bin').read_bytes())
        output = self.root / 'output.bin'
        output.write_bytes((one / 'golden.bin').read_bytes())
        self.assertTrue(adapter.verify(first, one, output)['passed'])
        output.write_bytes(b'\0' * (first['n'] * 4))
        self.assertFalse(adapter.verify(first, one, output)['passed'])
        output.write_bytes(struct.pack('<f', float('nan')) * first['n'])
        self.assertEqual(adapter.verify(first, one, output)['status'], 'OUTPUT_NONFINITE')
        output.write_bytes(b'x')
        self.assertEqual(adapter.verify(first, one, output)['status'], 'OUTPUT_SIZE_MISMATCH')
        (one / 'golden.bin').write_bytes(b'\0' * (first['n'] * 4))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            adapter.verify(first, one, output)

    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler unavailable')
    def test_real_compilation_execution_and_independent_full_output_check(self):
        package = load_problem(self.package_dir)
        adapter = package.load_adapter()
        project, build = self.root / 'project', self.root / 'build'
        package.stage_project(package.starter_path('optimize').read_bytes(), project)
        build.mkdir()
        for command in package.build_commands(project, build):
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
        for spec in adapter.cases('all', 7):
            judgedir = self.root / 'judge' / spec['case_id']
            inputs = self.root / 'execution' / spec['case_id']
            inputs.mkdir(parents=True)
            manifest = adapter.prepare(spec, judgedir)
            for name in manifest['inputs']:
                shutil.copyfile(judgedir / name, inputs / name)
            self.assertFalse((inputs / 'golden.bin').exists())
            output = inputs / 'output.bin'
            command = [str(package.runner_path(build)),
                       *adapter.runner_args(spec, inputs, output, mode='correctness', repeat=3, warmup=0)]
            run = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(len([row for row in rows if row['type'] == 'call']), 3)
            self.assertEqual(rows[-1]['status'], 'ok')
            result = adapter.verify(spec, judgedir, output, expected_calls=3)
            self.assertTrue(result['passed'], result)
            self.assertEqual(result['checked_captures'], 4)
            self.assertEqual(adapter.verify(spec, judgedir, output, expected_calls=4)['status'],
                             'CAPTURE_COUNT_MISMATCH')
            (inputs / 'output.bin.call-0').write_bytes(b'\0' * spec['n'] * 4)
            self.assertFalse(adapter.verify(spec, judgedir, output)['passed'])
        package.assert_unchanged()

    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler unavailable')
    def test_from_scratch_is_compilable_but_not_a_correct_solution(self):
        package = load_problem(self.package_dir)
        adapter = package.load_adapter()
        project, build = self.root / 'project', self.root / 'build'
        package.stage_project(package.starter_path('from_scratch').read_bytes(), project)
        build.mkdir()
        for command in package.build_commands(project, build):
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
        spec = adapter.cases('public', 0)[0]
        data = self.root / 'data'
        adapter.prepare(spec, data)
        output = self.root / 'output.bin'
        run = subprocess.run([str(package.runner_path(build)), *adapter.runner_args(spec, data, output)],
                             capture_output=True, text=True, timeout=10)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(adapter.verify(spec, data, output)['passed'])


if __name__ == '__main__':
    unittest.main()
