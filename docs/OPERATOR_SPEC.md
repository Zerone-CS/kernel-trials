# 算子题包接入规范

本规范面向题目作者和裁判维护者。待测 Agent 的交付始终只有 `kernel.asc`；数学语义、工程、输入生成、正确性 oracle、采样和评分由可信题包与评测后端提供。

题包是管理员安装的可执行代码，不是允许待测 Agent 在线上传的插件。下面描述当前实现的契约，依据 [题包加载器](../engine/problems.py)、[CPU 后端](../engine/runner.py)和 [NPU 后端](../engine/npu.py)。接口版本变化时，文档与测试应一起更新。

## 1. 接入范围

| 能力 | 当前支持情况 |
| --- | --- |
| 新算子的独立题包 | 支持；由题包定义 ABI、数据、oracle 和固定工程 |
| 新算子的完整题目评测 | 支持；为其创建新的 launcher，使用该 launcher 的 `full` 入口 |
| CPU 接口验证 | 支持 `cpu_smoke_only`；有编译与正确性结果，没有 NPU 性能分 |
| Ascend NPU 评测 | 支持符合当前 Ascend C / msprof 调用协议的题包；需真实硬件重新标定 |
| 任意新算子的 shape 自动发现 | 尚未实现；`configure-tasks`、`case01`–`case15` 和能力组当前专用于内置 BatchMatmulMaxSum |
| 多 kernel 调用的通用计时 | 尚未实现；当前 NPU 协议限制每次算子调用对应一个 device compute task |
| 恶意代码隔离、正式排名 | 尚未部署；源码检查和目录分离不构成 OS 安全边界 |

仅提交自然语言题目和不带测试契约的工程不足以自动评测。题目作者还需提供可执行的输入生成和独立验证接口。这些内容属于题包，不进入 Agent 的实现工作。

## 2. 目录布局

```text
problems/my_operator/
  problem.json
  task.md
  cases.json
  protocol.json
  judge.py
  harness/
    main.cpp                  # 或 Ascend C 固定 carrier
    CMakeLists.txt             # 根据工程需要提供
  public/
    operator_contract.h       # 可选公开接口
  starters/
    from_scratch/kernel.asc
    optimize/kernel.asc
  reference/
    kernel.asc                # NPU 基线标定需要
  tests/                      # 建议包含题目作者测试
```

目录名并非硬编码，具体文件由 `problem.json` 指向；提交文件名必须是 `kernel.asc`。`harness/` 不得预放这个文件，裁判在每次提交的独立工程快照中写入它。

所有文件，包括 README、测试和隐藏文件，都参与题包摘要。题包内禁止符号链接和特殊文件。已经注册或标定的题包不得写入缓存、编译产物或新测试数据；`__pycache__` 也会改变身份。adapter 由加载器直接编译，不应自行生成包内缓存。

## 3. `problem.json`

以下示例使用仓库中可运行的 CPU 样例结构。创建新题时修改 ID、版本、ABI、构建、数据和 oracle，不要只改题目名称。

```json
{
  "schema_version": 1,
  "id": "my_operator",
  "version": "1.0.0",
  "task": "task.md",
  "editable_file": "kernel.asc",
  "tracks": {
    "from_scratch": "starters/from_scratch/kernel.asc",
    "optimize": "starters/optimize/kernel.asc"
  },
  "harness": "harness",
  "public_files": {"operator_contract.h": "public/operator_contract.h"},
  "adapter": "judge.py",
  "cases": "cases.json",
  "protocol": "protocol.json",
  "reference": "reference/kernel.asc",
  "security": {"operator_profile": "cpu_example_untrusted"},
  "build": {
    "commands": [
      ["c++", "-std=c++14", "-O2", "{project}/main.cpp", "-o", "{build}/runner"]
    ],
    "runner": "runner"
  },
  "candidate_policy": {
    "maximum_source_bytes": 1048576,
    "allowed_include_headers": ["operator_contract.h", "cstddef", "cstdint", "cmath"]
  }
}
```

| 字段 | 契约 |
| --- | --- |
| `schema_version` | 当前必须为 `1` |
| `id`、`version` | 1–96 个字符，首字符为字母或数字，其余仅字母、数字、`_`、`.`、`-` |
| `editable_file` | 必须严格为 `kernel.asc` |
| `task`、`adapter`、`cases`、`protocol` | 指向存在的包内文件；adapter 必须是 `.py` 源文件，cases/protocol 必须是 JSON 对象 |
| `tracks` | 必须且只能包含 `from_scratch` 与 `optimize`，两者均指向存在的起始源码 |
| `harness` | 非空的固定工程目录，不包含 `kernel.asc` |
| `reference` | 加载器允许省略；当前 NPU 标定与正常 NPU 会话需要参考源码 |
| `public_files` | 可选；键是导出到工程的相对路径，值是包内源文件路径 |
| `build.commands` | 至少一个非空 argv 数组；依次执行，不经 shell |
| `build.runner` | 构建目录内的相对可执行文件路径 |
| `candidate_policy.maximum_source_bytes` | 1 到 16 MiB 的整数上限 |
| `candidate_policy.allowed_include_headers` | 允许的字面量 include 路径列表，不得重复 |
| `security.operator_profile` | 执行配置标签；不是启用隔离的开关，未知或正式模式限制由安全策略处理 |

包内路径必须使用规范的 `/` 相对路径，不允许绝对路径、反斜杠、空组件、`.`、`..` 或控制字符。`public_files` 不能覆盖 harness 中的文件、`kernel.asc`，也不能把 adapter、测试定义、协议、reference、起始代码或 `problem.json` 当成公开接口导出。

构建模板只支持 `{project}` 和 `{build}` 两个占位符。不要把一整条 shell 命令写成字符串，也不要假设参数中的 `&&`、`$VAR` 或重定向会被解释。构建依赖必须由管理员预先安装。

候选源码检查要求非空 UTF-8、无 NUL，并限制字面量 include；宏 include、`include_next` 和 `import` 会被拒绝。这不能证明任意 C++ 程序安全，也不能代替编译和运行隔离。

## 4. 题目与工程应说明什么

`task.md` 至少明确：

1. 数学定义，包括归约顺序、广播、转置、索引与边界语义。
2. ABI：函数签名、参数含义、输入输出布局、dtype、地址空间和同步责任。
3. 输入域：固定 shape 还是完整输入域，允许的特殊化和动态参数。
4. 精度：oracle 计算方式、量化时点、绝对/相对误差、NaN/Inf 处理。
5. 实现约束：只修改 `kernel.asc`，允许的头文件、内存与 kernel 调用约束。
6. 公布给 Agent 的性能场景和优化目标，不泄露隐藏输入的随机种子或答案。

固定 carrier 负责参数解析、设备初始化、内存分配、调用、同步、输出保存和运行元数据。待测文件负责题目要求的实现。`from_scratch` 可以提供缺实现的 ABI 骨架；`optimize` 必须是符合题意的正确起点。优化起点与性能参考可以不同，不能把已有起点的正确性算成 Agent 新增能力。

## 5. adapter 的四个基本接口

```python
def cases(suite, seed) -> list[dict]: ...
def prepare(spec, outdir) -> dict: ...
def runner_args(spec, datadir, output, *,
                mode="correctness", repeat=1, warmup=0) -> list[str]: ...
def verify(spec, datadir, output, *, expected_calls=None) -> dict: ...
```

adapter 是可信 Python 模块。使用自身 `__file__` 定位包内辅助文件，不依赖调用者当前目录、某个工作区路径或 `sys.path` 中的私有模块。导入时不得修改题包。具体参考 [CPU adapter](../problems/vector_add_cpu_example/judge.py) 和 [NPU adapter](../problems/batch_matmul_reduce/judge.py)。

### `cases(suite, seed)`

- 接收 `public`、`hidden` 或 `all`；返回非空使用场景列表，组合后的 `case_id` 应唯一。
- 每个 spec 包含完整生成参数，如 shape、dtype、layout、模式和派生 seed；`suite` 标记 `public` 或 `hidden`。
- 相同输入参数应得到相同列表与数据，不读取机器时间来暗中改变测试。
- 种子由裁判给定。当前 NPU 公开性能输入使用协议里的固定 `public_seed`，终评隐藏输入使用服务端种子。
- 方法对照在同任务、同重复轮次的不同条件间共享隐藏种子，不同轮次变化；adapter 应保留这一确定性，而不是重新随机抽样。

当前 NPU 后端对 `cases("public", seed)` 返回的**全部公开 case**进行性能测量。样例 spec 中的 `performance` 字段是题目数据的一部分，单独设为 `false` 不会让该 case 从 NPU 性能集合中自动消失。若新题需要公开但不计时的 case，需先扩展并测试后端契约。

### `prepare(spec, outdir)`

在裁判指定的新目录里生成输入和独立 golden，返回以下结构：

```json
{
  "inputs": ["input.bin"],
  "oracle_files": ["golden.bin"],
  "artifacts": {
    "input.bin": {"bytes": 128, "sha256": "文件内容的 SHA256"},
    "golden.bin": {"bytes": 128, "sha256": "文件内容的 SHA256"}
  },
  "output_dtype": "float32",
  "output_elements": 32
}
```

上例展示字段格式，摘要文字不是可执行测试数据。`inputs` 和 `oracle_files` 必须非空、互不重叠；输入名称不能重复。每个输入文件的字节数与摘要由框架重新核对，只将 `inputs` 复制到候选运行目录。adapter 应额外验证 golden 和缓存的身份，不能信任旧目录中同名文件。

当前输出边界支持 `float16`、`bfloat16`、`float32`、`float64`、`int8`、`uint8`、`int32`、`int64`。`output_elements` 必须为正整数，单个输出不得超过 64 MiB。多个逻辑输出需由题目定义确定的序列化格式，或先扩展后端；不能让 adapter 任意忽略文件大小检查。

### `runner_args(spec, datadir, output, ...)`

只返回参数数组，框架在前面添加可执行文件。`datadir` 指向仅有输入的目录，不是带 golden 的裁判目录。参数中不得加入 golden 路径。`repeat`、`warmup` 和 mode 由框架指定，不接受 Agent 私自调整。

固定 carrier 应写出最终 `output` 和每次调用的 `output.call-0`、`output.call-1` 等连续编号文件。正确性阶段需要 `repeat` 份 capture；性能阶段需要 `repeat + warmup` 份，预热输出也验证。框架检查这些文件存在、类型正常、数量及大小匹配；adapter 再检查实际数值。

### `verify(spec, datadir, output, ...)`

此处 `datadir` 是裁判拥有的原始数据目录，可读取 golden。返回字典且 `passed` 必须为布尔值；建议包含 `status`、`checked_elements`、`failed_elements`、`max_abs_error`、`checked_captures` 等诊断。公开反馈只返回后端白名单字段；隐藏 case 通常只反馈通过/失败。

必须以独立 oracle 和实际输出字节决定正确性，不接受候选 stdout 中自报的 PASS、耗时或误差。oracle 应覆盖量化后实际输入、完整输出、所有调用和非有限值。`expected_calls` 不是可以忽略的提示：正确的最后一次输出不能掩盖中间调用错误。

## 6. NPU 额外契约

NPU adapter 还必须实现：

```python
def validate_execution(spec, records, *, mode, repeat, warmup) -> dict:
    # 至少返回 passed；性能阶段还需要稳定、非空的 actual_cores。
    return {"passed": True, "actual_cores": 1,
            "status": "PASS", "infrastructure_failure": False}
```

这是返回结构示意，不能直接用“总是 True”的实现。`records` 是从固定 carrier 的日志逐行解析得到的 JSON 对象列表。内置实现检查一次 summary、调用次数与顺序、shape、dtype、设备、实际核数、预热标记，以及输入未修改、guard 正常、输出有限等条件。设备初始化错误可通过 `infrastructure_failure` 区分，不能误记为 Agent 实现失败。

数值正确性仍由 `verify` 独立判定。Host 输出的运行元数据不能证明恶意程序没有作弊。

`protocol.json` 中 NPU 后端实际读取的关键字段：

| 字段 | 用途 |
| --- | --- |
| `platform: "ascend_npu"` | 选择 NPU 后端 |
| `npu_arch` | 参与环境身份；构建命令中的架构参数还需由题包作者保持一致 |
| `runtime.device` | 非负设备编号，参与资源锁、环境身份和 profiler 核对 |
| `runtime.cores` | 冻结运行配置，内置 carrier 用 `0` 表示自动选择 |
| `public_seed` | 非负整数，默认 `0` |
| `correctness.repeats` | 1–1000；NPU 对每题取该值与 spec 的 `repeats` 的较大值 |
| `timing.metric` | 正常评分要求 `msprof_device_task_duration` |
| `timing.warmup_calls` | 非负整数 |
| `timing.measured_calls`、`independent_captures` | 正整数 |
| `limits.build_seconds`、`case_seconds`、`profile_seconds` | 正的有限秒数，各阶段上限 |
| `limits.operation_seconds` | 可选正整数，整次 worker 上限，默认 1800 秒 |

协议里的文字公式和 `atol`/`rtol` 不会自动生成评分器或 oracle。题目作者必须在 adapter 中真正实现精度规则，并与协议文本一致。当前评分公式由 [metrics.py](../engine/metrics.py) 实现；仅修改 JSON 的公式文字不会改变执行代码。

内置协议每 shape 独立采样 3 次，每次 8 次预热、32 次测量，取每次中位数后再取中位数。计时采集要求单设备、单计算流、相同 kernel 名、串行且每次调用对应一个 compute task；缺失、重叠或多 kernel capture 会无效，不用 host 耗时代替。不同调用策略若超出此契约，应先扩展测量关联与测试，再宣称支持。

## 7. 生命周期与题目作者的验证步骤

1. **从样例派生新目录。** CPU 接口参考 `problems/vector_add_cpu_example`；NPU 工程参考 `problems/batch_matmul_reduce`。保留既有题包原字节，给新包新的 ID/版本。
2. **写清任务与 ABI。** 确定两条轨道、输入域、精度与输出形式；固定 carrier 不应要求 Agent 修改工程。
3. **实现 adapter 与反例测试。** 验证正确实现通过，错误输出、边界、尾部、每次调用异常、文件缺失等被拒绝。
4. **校验包结构。** 在仓库根运行下面命令，路径换成新题包。

   ```sh
   python3 - <<'PY'
   from engine.problems import load_problem
   package = load_problem("problems/my_operator")
   adapter = package.load_adapter()
   print(package.problem_id, package.digest)
   print("public:", len(adapter.cases("public", 0)))
   print("all:", len(adapter.cases("all", 0)))
   package.assert_unchanged()
   PY
   ```

5. **运行相关回归。** 内置接口测试可先运行 `python3 -m unittest discover -s tests -p test_problems.py -v`；它验证内置样例，不能代替新题自己的测试。
6. **管理员配置可信环境。** 使用独立状态目录、节点本地 SQLite 和匹配的编译器/设备；启动服务的方法见 [README](../README.md)。CPU 只做流程验证；NPU 先执行 `admin.py calibrate` 获得新的参考测量。
7. **在新的部署目录绑定新题。** `admin.py configure-launcher --problem problems/my_operator ...` 创建其入口；已有 launcher 的身份不可原地替换。普通用户随后使用 `/op-eval`，该 launcher 的 `full` 就代表这个新题。
8. **做短预算验收。** 覆盖正确起点、编译错误、错误输出、check/bench/final、报告和预算收尾。流程 smoke 与 Agent 能力实验分开记录。

`configure-tasks` 不能用于任意新题的自动拆分。扩展其他算子的独立子任务时，需要题包生成器、选择器/标签、安装逻辑、独立身份与标定、报告分组，以及匹配的测试；不能把旧算子的 caseNN 名称指向不同语义后继续比较历史结果。

## 8. 版本、标定与发布验收

题目、starter、oracle、固定工程、输入集合或协议的任何字节变化都会改变 `problem_digest`。NPU 参考还绑定参考源码、计量引擎、协议、设备与环境身份。新子题不能从完整算子的旧时间向量摘出一项当成新标定。

提交题包变更前，应能回答：

- 正确参考是否通过完整公开和隐藏集合？错误实现是否确实失败？
- 精度规则是否基于实际量化输入，是否覆盖每个输出和每次调用？
- 新题是否仍只需提交 `kernel.asc`，harness/接口是否固定？
- case 数量、ID、时间向量、参考绑定是否一致？
- 是否明确说明固定 shape 特化与完整输入域的差别？
- 该变更是否需要新的 launcher 和重新标定？若需要，是否保存旧记录？
- 发布材料是否不含凭据、隐藏 seed、运行目录或私有日志？

正式账号隔离尚未部署，开源题包和同账号文件也不提供隐藏答案保密性。对照实验仍可用于可信研究开发，但不应把上述检查写成已实现正式防作弊的证明。

返回 [项目首页](../README.md) · [评测规则](../EVALUATION_POLICY.md) · [报告规范](REPORT_SPEC.md)
