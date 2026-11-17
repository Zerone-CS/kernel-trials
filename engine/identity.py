"""Identity of measurement code, distinct from workspace/report presentation."""
import hashlib
import json
from pathlib import Path


def measurement_engine_digest():
    root = Path(__file__).resolve().parent
    names = ("identity.py", "runner.py", "npu.py", "metrics.py", "calibration.py", "problems.py", "security.py",
             "execution.py", "worker.py")
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def protocol_sha256(package):
    return hashlib.sha256(package.path("protocol").read_bytes()).hexdigest()


def npu_resource_id(package):
    protocol = json.loads(package.path("protocol").read_text())
    if protocol.get("platform") != "ascend_npu":
        return None
    device = protocol.get("runtime", {}).get("device")
    if type(device) is not int or device < 0:
        raise ValueError("NPU package must freeze a nonnegative device id")
    return "ascend:npu:" + str(device)
