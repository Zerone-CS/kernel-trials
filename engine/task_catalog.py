"""Trusted, fixed-shape BatchMatmulMaxSum task materialization.

This catalog labels evaluation scenarios, not demonstrated independent abilities.
It never measures a device, imports a timing baseline, or alters the parent pack.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable

from .problems import ProblemError, load_problem


CATALOG_VERSION = 1
_SHAPE_KEYS = ("b", "m", "n", "k", "dtype", "ta", "tb")
_SHAPES = (
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
CAPABILITY_GROUPS = {
    "tiny_latency": (1,),
    "small_layout": (2, 3, 4),
    "batched_cube": (5, 6, 7),
    "tails": (2, 3, 6, 8),
    "large_gemm": (8, 9, 10, 11, 12),
    "long_m": (13,),
    "long_n": (14,),
    "deep_k": (15,),
}
_LABELS = (
    "单输出短点积", "交错小矩阵", "连续小矩阵与 N 尾部", "BF16 双转置小矩阵",
    "FP16 NN 批量矩阵", "BF16 NT 批量尾块", "FP16 TN 批量矩阵",
    "BF16 TT 大矩阵尾块", "FP16 NT 大矩阵", "BF16 NN 大矩阵",
    "FP16 TT 大矩阵", "BF16 TN 大矩阵", "长 M", "长 N", "深 K",
)


class TaskCatalogError(ValueError):
    """A selector, parent package or materialization destination is invalid."""


def list_tasks() -> list[dict]:
    """Return full plus 15 independently scored tasks; all values are fresh copies."""
    result = [{"id": "full", "label": "完整算子输入域与 15 个性能形状",
               "case_id": None, "capabilities": list(CAPABILITY_GROUPS),
               "shape": None, "scope": "full_operator"}]
    for index, (values, label) in enumerate(zip(_SHAPES, _LABELS), 1):
        shape = dict(zip(_SHAPE_KEYS, values))
        shape["layout"] = ("T" if shape["ta"] else "N") + ("T" if shape["tb"] else "N")
        result.append({"id": f"case{index:02d}", "label": f"Case {index:02d}：{label}",
                       "case_id": f"public_{index:02d}",
                       "capabilities": [name for name, members in CAPABILITY_GROUPS.items() if index in members],
                       "shape": shape, "scope": "fixed_shape"})
    return result


def resolve_tasks(selectors: str | Iterable[str] | None = None) -> list[str]:
    """Expand full/caseNN/group:<name>/all, preserving first occurrence order.

    Empty selection is full. ``all`` means only the 15 single-shape tasks; users
    wanting both contracts must explicitly request ``all,full``.
    """
    if selectors is None:
        return ["full"]
    if isinstance(selectors, str):
        selectors = [selectors]
    try:
        supplied = list(selectors)
    except TypeError as error:
        raise TaskCatalogError("Task selectors must be strings") from error
    if not supplied:
        return ["full"]
    tokens = []
    for value in supplied:
        if not isinstance(value, str):
            raise TaskCatalogError("Task selectors must be strings")
        parts = [part.strip() for part in value.split(",")]
        if any(not part for part in parts):
            raise TaskCatalogError("Empty task selector")
        tokens.extend(parts)
    known = {task["id"] for task in list_tasks()}
    result = []
    for token in tokens:
        if token == "all":
            expanded = [f"case{index:02d}" for index in range(1, 16)]
        elif token.startswith("group:") and token[6:] in CAPABILITY_GROUPS:
            expanded = [f"case{index:02d}" for index in CAPABILITY_GROUPS[token[6:]]]
        elif token in known:
            expanded = [token]
        else:
            raise TaskCatalogError("Unknown task selector: " + token)
        result.extend(task for task in expanded if task not in result)
    return result


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf8")


def _section(text, title):
    marker = "## " + title + "\n"
    if text.count(marker) != 1:
        raise TaskCatalogError("Parent task contract is missing section: " + title)
    body = text.split(marker, 1)[1].split("\n## ", 1)[0]
    return marker + body.rstrip() + "\n"


def _task_text(parent_text, task):
    shape = task["shape"]
    b, m, n, k = (shape[key] for key in ("b", "m", "n", "k"))
    x1 = [b, k, m] if shape["ta"] else [b, m, k]
    x2 = [b, n, k] if shape["tb"] else [b, k, n]
    mathematical = _section(parent_text, "数学语义")
    abi = _section(parent_text, "固定公共 ABI")
    abi = abi.replace("shape 为上表中的物理 shape", "shape 为本子任务指定的物理 shape")
    abi = abi.replace("因此两个输入为 1 或 2，输出为 0。",
                      f"本子任务两个输入为 {1 if shape['dtype'] == 'fp16' else 2}，输出为 0。")
    return f"""# BatchMatmulMaxSum / {task['label']}

这是固定 shape 特化任务，独立于 full 完整输入域任务。仅修改并交付 `kernel.asc`；
固定工程、ABI、编译文件和裁判由评测框架提供。无需实现其它 shape、输入类型或转置组合。
允许按下列固定规格进行 tiling、展开、模板或内存布局特化；不得硬编码输入值、随机种子、
golden 或输出答案，也不得缓存答案、跳过计算或篡改测量。此成绩不代表 shape 泛化能力。
能力标签 {', '.join(task['capabilities'])} 仅用于筛选场景，不是唯一优化方案或已证实的瓶颈。

两轨道接口相同：`from_scratch` 从未实现入口开始；`optimize` 从通用动态分块正确 kernel
开始。优化起点不是最终参考实现。Host 校验、tiling 和工作区管理可以写在 kernel.asc，
张量运算须在 NPU Device 完成。不要添加 main 或重复定义载体提供的 TensorInfo/TensorGroupInfo。

## 本子任务唯一输入规格

| B | M | N | K | dtype | transposeX1 | transposeX2 | 布局 |
|---:|---:|---:|---:|---|---|---|---|
| {b} | {m} | {n} | {k} | {shape['dtype'].upper()} | {str(shape['ta']).lower()} | {str(shape['tb']).lower()} | {shape['layout']} |

X1 物理 shape 为 `{x1}`，X2 为 `{x2}`；输入均连续 ND，输出为连续 FP32 `[{b}]`。
transpose 属性声明物理存储布局，不改变逻辑数学语义。输入有效值有限且可以为负；
输入及分配边界不得修改，输出必须全部写入且不依赖原有内容。

{mathematical}
{abi}
## 正确性与测量

裁判用实际量化存储的输入值进行 FP64 点积、Max(N)、Sum(M)，最后转 FP32 golden。
每个输出必须有限且满足 `abs(actual-golden) <= 1e-4 + 1e-4*abs(golden)`。
公开反馈使用本规格的随机输入；终评另外检查同一规格的重新采样随机、全负、零、
有限幅度抵消输入。重复调用逐次验证，检查输入不变、输出写入和边界保护。
随机重复检查至少 16 次，其余每类至少 3 次。终评不会调用父题的其它 24 个边界 shape。
隐藏表示终评反馈保留到最后，并不声称输入分布本身保密。

性能只测本 shape，以 msprof 设备计算任务时长为准；Host 时间仅作诊断。
子任务需要按自己的题包、协议、硬件与测量引擎身份重新标定参考，不能截取父题旧时间。
参考是已知实现 baseline，不是真实理论最优；分数只包含本 shape，分组不产生新的混合分数。
开发模式已支持 NPU check/bench/final 与评分，实际可用性由服务响应决定；正式防作弊隔离
仍未部署，开发成绩不能作为正式隔离排行榜成绩。预算和终止规则由外部框架管理。
"""


_PACKAGE_TEST = '''"""Metadata check for this generated fixed-shape package; no NPU execution."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FixedShapeCatalogTests(unittest.TestCase):
    def test_only_declared_shape_and_all_input_patterns(self):
        task = json.loads((ROOT / "problem.json").read_text())["evaluation_task"]
        cases = json.loads((ROOT / "cases.json").read_text())
        self.assertEqual(len(cases["public"]), 1)
        self.assertEqual(len(cases["hidden"]), 4)
        for case in cases["public"] + cases["hidden"]:
            for key in ("b", "m", "n", "k", "dtype", "ta", "tb"):
                self.assertEqual(case[key], task["shape"][key])
        self.assertEqual({case["pattern"] for case in cases["hidden"]},
                         {"random", "all_negative", "zero", "cancellation"})


if __name__ == "__main__":
    unittest.main()
'''


def _derived_files(base, task):
    if base.problem_id != "batch_matmul_reduce":
        raise TaskCatalogError("Single-shape catalog requires the batch_matmul_reduce parent package")
    catalog = json.loads(base.path("cases").read_text())
    public = catalog.get("public")
    if not isinstance(public, list) or len(public) != len(_SHAPES):
        raise TaskCatalogError("Parent public catalog differs from the 15-shape task catalog")
    for index, (case, expected) in enumerate(zip(public, _SHAPES), 1):
        if (case.get("case_id") != f"public_{index:02d}"
                or tuple(case.get(key) for key in _SHAPE_KEYS) != expected):
            raise TaskCatalogError("Parent public shape differs from the task catalog")
    selected = deepcopy(public[int(task["id"][4:]) - 1])
    selected.update(pattern="random", repeats=max(3, selected.get("repeats", 3)))
    hidden = []
    for index, pattern in enumerate(("random", "all_negative", "zero", "cancellation"), 1):
        hidden.append(dict(selected, case_id=f"{task['id']}_hidden_{pattern}", pattern=pattern,
                           seed=selected["seed"] + 100000 + index,
                           repeats=max(16 if pattern == "random" else 3, selected["repeats"])))
    cases = {"public": [selected], "hidden": hidden,
             "hidden_distribution_note": "Fixed shape/dtype/transpose only; final withholds feedback and resamples seeds. No parent boundary shapes are used."}
    manifest = deepcopy(base.manifest)
    manifest["id"] = "batch_matmul_reduce_" + task["id"]
    manifest["version"] = "2.0.0-fixed-shape-v1"
    manifest["evaluation_task"] = deepcopy(task)
    protocol = json.loads(base.path("protocol").read_text())
    protocol["name"] = "batch_matmul_reduce_v2_" + task["id"] + "_fixed_shape_v1"
    protocol["evaluation_task"] = deepcopy(task)
    protocol["score"].update(
        aggregate="arithmetic mean across the selected public performance shape (1 case)",
        requires_fresh_calibration=True,
        reason="Independent fixed-shape package identity; recalibrate the preserved full reference source. Parent timings are not a child baseline.")
    protocol["limitations"] = [
        "Fixed shape, dtype and transpose specialization; no shape-generalization claim",
        "Input distributions are inspectable; only final seeds/input realizations are resampled",
        "No hostile Host-code isolation; development score is not cheat-resistant",
        "Repeated same-shape calls are checked; cross-shape transitions are outside this task",
    ]
    provenance = {"schema_version": CATALOG_VERSION, "generator": "engine.task_catalog",
                  "parent_problem_id": base.problem_id, "parent_package_digest": base.digest,
                  "evaluation_task": deepcopy(task), "requires_fresh_calibration": True,
                  "reference_source_sha256": hashlib.sha256(base.path("reference").read_bytes()).hexdigest(),
                  "preserved_files": [base.manifest["reference"], *base.manifest["tracks"].values(),
                                      base.manifest["adapter"], "trusted/cases.py"],
                  "group_semantics": "selection_only_no_group_score"}
    files = {relative: (base.root / relative).read_bytes() for relative in base.file_hashes}
    files.update({"problem.json": _json_bytes(manifest), base.manifest["cases"]: _json_bytes(cases),
                  base.manifest["protocol"]: _json_bytes(protocol),
                  base.manifest["task"]: _task_text(base.path("task").read_text(), task).encode("utf8"),
                  "provenance/task.json": _json_bytes(provenance),
                  "tests/test_adapter.py": _PACKAGE_TEST.encode("utf8"),
                  "README.md": (f"# {task['label']}\n\nIndependent fixed-shape package generated from "
                                f"`{base.problem_id}`. See task.md and provenance/task.json.\n"
                                "Only one public performance shape and four same-shape final input patterns are evaluated.\n"
                                "The full reference and both starters are preserved; reference is never a public workspace file.\n"
                                "Calibrate this package independently. Capability groups only select tasks, never combine scores.\n"
                                "No NPU validation is implied by materialization.\n").encode("utf8")})
    base.assert_unchanged()
    return files


def _safe_destination(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise TaskCatalogError("Task destination must not traverse a symlink")
    return path.resolve()


@contextmanager
def _materialization_lock(destination):
    path = destination.parent / ("." + destination.name + ".materialize.lock")
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def materialize_task(base_problem: Path, task_id: str, destination: Path) -> Path:
    """Create an independent complete trusted pack; reuse only byte-identical packs.

    ``full`` validates and returns the original directory without writing anything.
    Child output must be outside that directory. Existing different content is never
    overwritten. New output is assembled privately and atomically renamed in place.
    """
    task = next((item for item in list_tasks() if item["id"] == task_id), None)
    if task is None:
        raise TaskCatalogError("Materialization requires one full/caseNN task id")
    base = load_problem(base_problem)
    if task_id == "full":
        return base.root
    target = _safe_destination(destination)
    if target == base.root or base.root in target.parents:
        raise TaskCatalogError("Child package must be outside the immutable parent package")
    files = _derived_files(base, task)
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    target.parent.mkdir(parents=True, exist_ok=True)
    with _materialization_lock(target):
        if target.exists():
            try:
                existing = load_problem(target)
            except (OSError, ProblemError) as error:
                raise TaskCatalogError("Existing task destination is not the identical generated package") from error
            if existing.file_hashes != expected:
                raise TaskCatalogError("Existing task destination differs; refusing overwrite")
            return existing.root
        temporary = Path(tempfile.mkdtemp(prefix="." + target.name + "-", dir=target.parent))
        try:
            for relative, data in files.items():
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                original = base.root / relative
                if original.is_file():
                    path.chmod(original.stat().st_mode & 0o777)
            load_problem(temporary)
            base.assert_unchanged()
            os.rename(str(temporary), str(target))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target
