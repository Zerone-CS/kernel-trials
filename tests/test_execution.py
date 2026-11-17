import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from engine.execution import run_managed
from engine.problems import load_problem


FIXTURE = Path(__file__).resolve().parents[1] / "problems/vector_add_cpu_example"


@unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
class SupervisionTests(unittest.TestCase):
    def test_whole_operation_timeout_terminates_active_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(FIXTURE, root / "package")
            path = root / "package/protocol.json"
            protocol = json.loads(path.read_text())
            protocol["limits"].update(operation_seconds=2, case_seconds=60)
            path.write_text(json.dumps(protocol))
            package = load_problem(root / "package")
            source = b"void run_kernel(const float*,const float*,float*,std::size_t){volatile int x=0;while(true){x++;}}"
            report = run_managed(package, source, "check", root / "run")
            self.assertEqual(report["error_category"], "operation_timeout")
            self.assertIsNone(report["score"])
            self.assertTrue(report["worker"]["timed_out"])
            processes = subprocess.check_output(["ps", "-Ao", "args"], text=True)
            self.assertNotIn(str(root / "run/build/runner"), processes)
