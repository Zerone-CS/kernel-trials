"""Deterministic BatchMatmulMaxSum inputs and a stored-value FP64 oracle.

This is a local evaluation protocol, not a reproduction of CANNJudge's private
input distribution. NumPy and ml_dtypes are imported only when generating data.
The catalog and manifest specification can be inspected with the standard library.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Iterable


SCHEMA = "op-eval-cases-v1"
DEFAULT_SEED = 24681357
ATOL = 1e-4
RTOL = 1e-4
PATTERNS = ("random", "all_negative", "zero", "cancellation")
# B, M, N, K, dtype, transposeX1, transposeX2. These are shapes, not 15 operators.
PUBLIC_SHAPES = (
    (1, 1, 1, 32, "fp16", False, False),
    (1, 3, 5, 40, "bf16", True, False),
    (2, 7, 13, 48, "fp16", False, True),
    (4, 8, 16, 128, "bf16", True, True),
    (8, 16, 32, 128, "fp16", False, False),
    (13, 23, 73, 192, "bf16", False, True),
    (16, 32, 128, 256, "fp16", True, False),
    (1, 1025, 1031, 1032, "bf16", True, True),
    (1, 2048, 1536, 1280, "fp16", False, True),
    (1, 4096, 1280, 1152, "bf16", False, False),
    (1, 1536, 2048, 2048, "fp16", True, True),
    (1, 1280, 4096, 1536, "bf16", True, False),
    (1, 8192, 64, 128, "fp16", False, False),
    (1, 64, 8192, 128, "bf16", False, True),
    (1, 64, 64, 8192, "fp16", True, True),
)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    b: int
    m: int
    n: int
    k: int
    dtype: str
    ta: bool = False
    tb: bool = False
    seed: int = DEFAULT_SEED
    pattern: str = "random"
    repeats: int = 3

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.case_id):
            raise ValueError("case_id must be a single safe path component")
        limits = {"b": (1, 64), "m": (1, 8192), "n": (1, 8192),
                  "k": (32, 8192), "seed": (0, 2**64 - 1), "repeats": (1, 100000)}
        for key, (lo, hi) in limits.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} must be an integer in [{lo}, {hi}]")
        if self.k % 8 or self.b * self.m * self.k > 2**26 or self.b * self.n * self.k > 2**26:
            raise ValueError("K alignment or input element limit violated")
        if self.dtype not in ("fp16", "bf16") or self.pattern not in PATTERNS:
            raise ValueError("unsupported dtype or pattern")
        if type(self.ta) is not bool or type(self.tb) is not bool:
            raise ValueError("ta and tb must be booleans")

    @property
    def x1_shape(self):
        return (self.b, self.k, self.m) if self.ta else (self.b, self.m, self.k)

    @property
    def x2_shape(self):
        return (self.b, self.n, self.k) if self.tb else (self.b, self.k, self.n)


def public_cases(seed: int = DEFAULT_SEED, repeats: int = 3) -> list[CaseSpec]:
    return [CaseSpec(f"public_{i:02d}", *shape, seed=seed + 101 * i, repeats=repeats)
            for i, shape in enumerate(PUBLIC_SHAPES, 1)]


def boundary_cases(seed: int = DEFAULT_SEED) -> list[CaseSpec]:
    """24 small cases: layouts, patterns, historical failures, batches and reentry.

    Reentry coverage requires a runner that keeps one process/context for this
    ordered suite. The manifest alone does not establish that runtime behavior.
    """
    cases = []

    def add(name, shape, pattern="random", repeats=3):
        cases.append(CaseSpec(name, *shape, seed=seed + 10000 + len(cases),
                              pattern=pattern, repeats=repeats))

    for dtype in ("fp16", "bf16"):
        for ta in (False, True):
            for tb in (False, True):
                add(f"layout_{dtype}_{int(ta)}{int(tb)}", (2, 3, 5, 40, dtype, ta, tb))
    for dtype in ("fp16", "bf16"):
        for pattern in ("all_negative", "zero", "cancellation"):
            add(f"pattern_{dtype}_{pattern}", (2, 3, 7, 40, dtype, True, False), pattern)
    for dtype in ("fp16", "bf16"):
        add(f"case1_repeat_{dtype}", (1, 1, 1, 32, dtype, False, False), repeats=16)
    for pattern in ("random", "all_negative"):
        add(f"case6_m1_n65_{pattern}", (1, 1, 65, 32, "bf16", False, True), pattern, 4)
    for name, shape in (
        ("batch64", (64, 1, 1, 32, "fp16", False, True)),
        ("tails17_33", (3, 17, 33, 40, "bf16", True, True)),
        ("tails31_17", (1, 31, 17, 72, "fp16", False, False)),
        ("aligned16_k40", (2, 16, 16, 40, "bf16", True, False)),
    ):
        add(name, shape)
    for dtype in ("fp16", "bf16"):
        add(f"case1_reentry_{dtype}", (1, 1, 1, 32, dtype, False, False), repeats=8)
    return cases


def _libraries():
    import numpy as np
    import ml_dtypes
    return np, ml_dtypes


def make_inputs(spec: CaseSpec):
    """Return contiguous, quantized storage-layout arrays x1 and x2.

    Random values are bounded by 0.5. Cancellation uses exact small binary
    fractions with paired K terms and a residual, avoiding overflow stress.
    """
    np, ml_dtypes = _libraries()
    rng = np.random.Generator(np.random.PCG64(spec.seed))
    a_shape, b_shape = (spec.b, spec.m, spec.k), (spec.b, spec.k, spec.n)
    if spec.pattern == "zero":
        a, b = np.zeros(a_shape, np.float32), np.zeros(b_shape, np.float32)
    elif spec.pattern == "cancellation":
        # Every adjacent K pair cancels; the last odd term leaves a small residue.
        a = rng.integers(1, 5, size=(spec.b, spec.m, spec.k // 2)).astype(np.float32) / 8
        a = np.repeat(a, 2, axis=2)
        b = rng.integers(1, 5, size=(spec.b, spec.k // 2, spec.n)).astype(np.float32) / 8
        b = np.repeat(b, 2, axis=1)
        b[:, 1::2, :] *= -1
        b[:, -1, :] += np.float32(1 / 32)
    else:
        a = rng.random(a_shape, dtype=np.float32)
        b = rng.random(b_shape, dtype=np.float32)
        if spec.pattern == "all_negative":
            a *= np.float32(3 / 8)
            a += np.float32(1 / 8)
            b *= np.float32(-3 / 8)
            b -= np.float32(1 / 8)
        else:
            a -= np.float32(0.5)
            b -= np.float32(0.5)
    dtype = np.float16 if spec.dtype == "fp16" else ml_dtypes.bfloat16
    a = a.astype(dtype)
    b = b.astype(dtype)
    if spec.ta:
        a = a.transpose(0, 2, 1)
    if spec.tb:
        b = b.transpose(0, 2, 1)
    return np.ascontiguousarray(a), np.ascontiguousarray(b)


def golden_reference(spec: CaseSpec, x1, x2, row_chunk: int = 32,
                     max_temp_bytes: int = 64 * 1024 * 1024):
    """FP64 dot → max(N) → sum(M), then FP32, using actual quantized values.

    Row and column blocking bounds explicit temporary arrays; BLAS may allocate
    its own workspace. This is a CPU reference and is never part of kernel timing.
    """
    np, ml_dtypes = _libraries()
    expected_dtype = np.dtype(np.float16 if spec.dtype == "fp16" else ml_dtypes.bfloat16)
    if x1.shape != spec.x1_shape or x2.shape != spec.x2_shape:
        raise ValueError("input storage shape mismatch")
    if x1.dtype != expected_dtype or x2.dtype != expected_dtype:
        raise ValueError("oracle requires actual quantized input arrays")
    if row_chunk < 1 or max_temp_bytes < 8 * (spec.k + 1):
        raise ValueError("invalid reference chunk budget")
    if not np.isfinite(x1).all() or not np.isfinite(x2).all():
        raise ValueError("input contains a nonfinite value")
    a = x1.transpose(0, 2, 1) if spec.ta else x1
    b = x2.transpose(0, 2, 1) if spec.tb else x2
    rows = min(row_chunk, spec.m)
    # Reserve the A chunk, B chunk and result chunk inside the explicit budget.
    available = max_temp_bytes - 8 * rows * spec.k
    if available < 8 * (spec.k + rows):
        raise ValueError("reference budget too small for one column and row chunk")
    cols = min(spec.n, available // (8 * (spec.k + rows)))
    result = np.empty(spec.b, dtype=np.float64)
    for batch in range(spec.b):
        row_max = np.full(spec.m, -np.inf, dtype=np.float64)
        for col in range(0, spec.n, cols):
            b64 = np.asarray(b[batch, :, col:col + cols], dtype=np.float64, order="C")
            for row in range(0, spec.m, rows):
                end = min(row + rows, spec.m)
                a64 = np.asarray(a[batch, row:end, :], dtype=np.float64, order="C")
                product = a64 @ b64
                np.maximum(row_max[row:end], product.max(axis=1), out=row_max[row:end])
        result[batch] = row_max.sum(dtype=np.float64)
    result = result.astype(np.float32)
    if not np.isfinite(result).all():
        raise ValueError("reference output is not finite FP32")
    return result


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _generator_hash():
    return sha256_file(Path(__file__))


def _write_json(path, data):
    with Path(path).open("x", encoding="utf8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _check_artifacts(manifest, case_dir):
    for artifact in manifest["artifacts"].values():
        path = case_dir / artifact["file"]
        if path.stat().st_size != artifact["bytes"] or sha256_file(path) != artifact["sha256"]:
            raise ValueError(f"artifact identity mismatch: {path}")


def generate_case(spec: CaseSpec, out_dir: Path | str, row_chunk: int = 32,
                  max_temp_bytes: int = 64 * 1024 * 1024) -> dict:
    """Generate one case directory, or reuse a verified identical cached case."""
    np, ml_dtypes = _libraries()
    out_dir = Path(out_dir)
    manifest_path = out_dir / "case.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf8"))
        if (manifest.get("schema") != SCHEMA or manifest.get("spec") != asdict(spec)
                or manifest.get("generator_sha256") != _generator_hash()):
            raise ValueError(f"cached case does not match generator/spec: {manifest_path}")
        _check_artifacts(manifest, out_dir)
        return manifest
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError(f"refusing to overwrite incomplete or unrelated case: {out_dir}")
    x1, x2 = make_inputs(spec)
    golden = golden_reference(spec, x1, x2, row_chunk, max_temp_bytes)
    # Use uint16 views for BF16: NumPy .npy readers need not know ml_dtypes.
    for name, value in (("x1", x1), ("x2", x2)):
        value.view(np.uint16).astype("<u2", copy=False).tofile(out_dir / f"{name}.bin")
    golden.astype("<f4", copy=False).tofile(out_dir / "golden.bin")
    artifacts = {}
    for name in ("x1", "x2", "golden"):
        path = out_dir / f"{name}.bin"
        artifacts[name] = {"file": path.name, "bytes": path.stat().st_size,
                           "sha256": sha256_file(path)}
    manifest = {
        "schema": SCHEMA, "case_id": spec.case_id, "spec": asdict(spec),
        "b": spec.b, "m": spec.m, "n": spec.n, "k": spec.k,
        "ta": spec.ta, "tb": spec.tb, "dtype": spec.dtype,
        "dtype_code": 1 if spec.dtype == "fp16" else 2,
        "seed": spec.seed, "pattern": spec.pattern, "repeats": spec.repeats,
        "x1_shape": list(spec.x1_shape), "x2_shape": list(spec.x2_shape),
        "output_shape": [spec.b], "output_dtype": "float32", "byte_order": "little",
        "x1_file": "x1.bin", "x2_file": "x2.bin", "golden_file": "golden.bin",
        "artifacts": artifacts, "generator_sha256": _generator_hash(),
        "rng": "numpy.PCG64", "numpy_version": np.__version__,
        "ml_dtypes_version": ml_dtypes.__version__,
        "reference": "stored_values_fp64_matmul_max_n_sum_m_then_fp32",
        "comparison": {"atol": ATOL, "rtol": RTOL, "formula": "abs(actual-golden) <= atol + rtol*abs(golden)"},
        "runtime_note": "repeats and ordered reentry require runner enforcement",
    }
    _write_json(manifest_path, manifest)
    return manifest


def generate_suite(out_dir: Path | str, cases: Iterable[CaseSpec], row_chunk: int = 32,
                   max_temp_bytes: int = 64 * 1024 * 1024) -> dict:
    cases = list(cases)
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("suite must have unique nonempty cases")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf8"))
        if existing.get("case_specs") != [asdict(case) for case in cases]:
            raise ValueError("cached suite case order/spec mismatch")
    records = []
    for spec in cases:
        manifest = generate_case(spec, out_dir / spec.case_id, row_chunk, max_temp_bytes)
        records.append({**manifest, "case_dir": spec.case_id,
                        "case_manifest": f"{spec.case_id}/case.json"})
    result = {"schema": SCHEMA, "operator": "BatchMatmulMaxSum", "cases": records,
              "case_count": len(cases), "case_specs": [asdict(case) for case in cases],
              "generator_sha256": _generator_hash(),
              "total_public_calls": sum(case.repeats for case in cases),
              "is_hidden_test_suite": False}
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf8")) != result:
            raise ValueError("cached suite metadata mismatch")
    else:
        _write_json(manifest_path, result)
    return result


def compare_outputs(golden, actual) -> dict:
    """Check exact shape, FP32 dtype, finiteness, and every output element."""
    np, _ = _libraries()
    golden, actual = np.asarray(golden), np.asarray(actual)
    if actual.shape != golden.shape:
        return {"passed": False, "status": "OUTPUT_SHAPE_MISMATCH",
                "expected_shape": list(golden.shape), "actual_shape": list(actual.shape)}
    if actual.dtype != np.dtype("float32"):
        return {"passed": False, "status": "OUTPUT_DTYPE_MISMATCH", "actual_dtype": str(actual.dtype)}
    if not np.isfinite(golden).all():
        raise ValueError("invalid nonfinite golden")
    finite = np.isfinite(actual)
    if not finite.all():
        return {"passed": False, "status": "OUTPUT_NONFINITE",
                "nonfinite_elements": int((~finite).sum())}
    error = np.abs(actual.astype(np.float64) - golden.astype(np.float64))
    tolerance = ATOL + RTOL * np.abs(golden.astype(np.float64))
    failed = error > tolerance
    return {"passed": not bool(failed.any()), "status": "PASS" if not failed.any() else "OUTPUT_MISMATCH",
            "checked_elements": int(actual.size), "failed_elements": int(failed.sum()),
            "max_abs_error": float(error.max(initial=0)),
            "max_tolerance_ratio": float((error / tolerance).max(initial=0)),
            "atol": ATOL, "rtol": RTOL}


def verify_output(case_manifest: Path | str, output_path: Path | str, repeats: int = 1) -> dict:
    """Verify y.bin containing one or explicitly requested consecutive [B] outputs.

    A single-output check does not certify every runtime iteration. Pass repeats
    only when the runner wrote all iterations in call-major order.
    """
    np, _ = _libraries()
    path = Path(case_manifest)
    manifest = json.loads(path.read_text(encoding="utf8"))
    if manifest.get("schema") != SCHEMA or isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("invalid schema or repeat count")
    spec = CaseSpec(**manifest["spec"])
    _check_artifacts(manifest, path.parent)
    expected_bytes = repeats * spec.b * 4
    actual_bytes = Path(output_path).stat().st_size
    if actual_bytes != expected_bytes:
        return {"passed": False, "status": "OUTPUT_SIZE_MISMATCH", "case_id": spec.case_id,
                "expected_bytes": expected_bytes, "actual_bytes": actual_bytes}
    golden = np.fromfile(path.parent / manifest["golden_file"], dtype="<f4")
    if golden.shape != (spec.b,):
        raise ValueError("golden file shape mismatch")
    actual = np.fromfile(output_path, dtype="<f4").reshape(repeats, spec.b)
    expected = np.broadcast_to(golden, (repeats, spec.b))
    return {**compare_outputs(expected, actual), "case_id": spec.case_id,
            "verified_repeats": repeats, "output_sha256": sha256_file(output_path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-suite")
    generate.add_argument("--output", required=True)
    generate.add_argument("--suite", choices=("public", "boundary", "all"), default="all")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--row-chunk", type=int, default=32)
    generate.add_argument("--max-temp-mib", type=int, default=64)
    generate.add_argument("--public-repeats", type=int, default=3)
    generate.add_argument("--manifest-only", action="store_true", help="print case specifications without generating arrays")
    verify = commands.add_parser("verify")
    verify.add_argument("--case", required=True, help="path to case.json")
    verify.add_argument("--output", required=True, help="path to FP32 y.bin")
    verify.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-suite":
            cases = []
            if args.suite in ("public", "all"):
                cases += public_cases(args.seed, args.public_repeats)
            if args.suite in ("boundary", "all"):
                cases += boundary_cases(args.seed)
            if args.manifest_only:
                print(json.dumps({"schema": SCHEMA, "case_count": len(cases),
                                  "case_specs": [asdict(case) for case in cases]}, indent=2))
            else:
                result = generate_suite(args.output, cases, args.row_chunk, args.max_temp_mib * 1024 * 1024)
                print(json.dumps({"status": "GENERATED", "case_count": result["case_count"],
                                  "manifest": str(Path(args.output) / "manifest.json")}))
            return 0
        result = verify_output(args.case, args.output, args.repeats)
        print(json.dumps(result, allow_nan=False))
        return 0 if result["passed"] else 1
    except (ValueError, OSError, ImportError, KeyError) as exc:
        print(json.dumps({"passed": False, "status": "EVALUATOR_ERROR", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
