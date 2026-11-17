# 报告、评分与可复现记录规范

本规范面向报告使用者、统计分析脚本和评测维护者。最终结论必须来自裁判受理的源码快照和独立终评；Agent 的文字总结、CLI 退出码或某次公开 bench 不能代替最终成绩。

本文描述当前 `schema_version: 1` 的单场报告和实验比较报告，以及 `schema_version: 2` 的参考 baseline。它是当前实现的字段说明，不承诺所有后续版本都保持不变。分析程序应核对 `kind`、schema 版本和身份字段，保留未知字段，并拒绝把缺失值补成 0。

实现依据：[reporting.py](../engine/reporting.py)、[controller.py](../engine/controller.py)、[comparison.py](../engine/comparison.py)、[calibration.py](../engine/calibration.py)。

## 1. 产物层级与权威来源

| 文件/对象 | 含义 | 用途 |
| --- | --- | --- |
| 工作区 `REPORT.md`、`.eval/report.json` | 单场裁判报告的可读与结构化版本 | 判断该会话的唯一终评 |
| 工作区 `.eval/last.json` | 最近一次 API 反馈 | 调试与开发，不等于终评 |
| 实验 `manifest.json` | 服务端冻结的实验计划、任务和条件身份 | 定义全部应运行 trial |
| 实验 `trials.json` | 全部计划 trial，含已获取的裁判报告和执行元数据 | 审计失败、缺失、每场来源 |
| 实验 `comparison.json`、`REPORT.md` | 根据 manifest 和全部 trial 生成的归一化统计 | 任务、能力标签与配对差异 |
| `trials/<trial_id>/report.json`、`REPORT.md` | 单场终评的实验目录副本 | 回溯具体结果 |
| `trials/<trial_id>/execution.json`、`logs/` | CLI 状态、耗时、提供方元数据与私有日志 | 排查进程问题，不作为算子成绩 |
| 裁判状态中的 baseline JSON | 校准后的参考实现与采样证据 | 冻结 `t_reference`，不是某个方法条件 |

实验目录通常位于仓库父目录的 `op_eval_experiment_runs/<experiment_id>/`。远程 compare 会将 manifest、trials、comparison、REPORT 四份汇总文件下载到本机 `.eval/remote-experiments/<experiment_id>/`；完整试验目录仍在服务器。返回值中的路径以实际运行位置为准。

HTTP `report` 响应是 `{"report": {...}, "markdown": "..."}` 的 envelope。落盘的 `report.json` 通常是内层报告对象。不要把两层格式混淆。实验驱动从裁判获取结果，不读取候选自己改写的本地报告来评分。

## 2. 单场报告

根对象包含：

```text
schema_version = 1
kind = "operator_evaluation_report"
summary
session
budget
result
comparison
diagnostics
```

### `session`：会话与任务身份

| 字段 | 含义 |
| --- | --- |
| `session_id` | 一场独立评测的唯一标识 |
| `state` | 权威会话状态，如 PENDING、OPEN、FINISHED、EXPIRED、CANCELLED |
| `problem_digest` | 题包所有文件及路径的内容摘要 |
| `track` | `from_scratch` 或 `optimize` |
| `launcher_id` | 分配来源，可为空 |
| `attempt_index` | 普通 launcher 的历史尝试序号，或研究计划写入的 trial 顺序号；不能当作重复实验的轮次 |
| `evaluation_purpose` | `agent_evaluation`、`entry_smoke` 或旧记录的未指定值 |
| `purpose_source` | 当前值为 `administrator_bound_session_metadata` |
| `agent_metadata_verified` | 当前不认证真实模型身份 |
| `claimed_agent_metadata` | controller 补充的会话元数据；实验可含 experiment/trial/task/condition/repeat/method 摘要 |

`entry_smoke` 只能验证入口和后端。即使有有效 NPU 分数，也不能声称该分数证明 Agent 实现或优化能力。

### `budget`：预算与时间

| 字段 | 含义 |
| --- | --- |
| `policy.max_feedback` | check/bench 的反馈上限 |
| `policy.active_seconds` | 取得资源后开始的任务时间上限 |
| `policy.final_grace_seconds` | 选择已有提交进行终评的宽限，不是新增开发时间 |
| `feedback_used`、`feedback_remaining` | 服务端已消耗/剩余反馈；编译失败也计数 |
| `started_at`、`stop_at`、`completed_at` | 服务端时间戳；未发生的事件可以为 null |
| `budget_stop_reason` | 例如 feedback_limit、time_limit；提前终评可没有耗尽原因 |
| `session_elapsed_seconds_including_final` | 从成功 start 到终评完成的耗时，包含终评，不等同于有效开发时间 |

预算默认 20 次反馈、3600 秒任务时间、600 秒终评选择宽限。重试同一个已受理请求不重复计数。完整停止规则见 [评测规则](../EVALUATION_POLICY.md)。

### `result`：唯一终评

| 字段 | 含义 |
| --- | --- |
| `outcome` | passed、compile_failed、runtime_failed、correctness_failed、infrastructure_error、measurement_invalid、formal_unavailable、no_final_result 等 |
| `score` | 有效最终任务分、合法候选失败的 0，或 null |
| `score_status` | 后端的分数状态/不可用原因；结合 outcome 与 stages 阅读 |
| `source_sha256` | 实际终评的历史源码快照摘要；未终评为 null |
| `final_operation_id` | 唯一终评操作 ID；未终评为 null |
| `stages` | build、runtime、correctness、performance 各阶段状态 |
| `correctness` | passed、total、executed、scope；没有执行时可能缺少数值 |
| `error_category` | 更具体的错误类型，不包含隐藏 oracle 内容 |
| `metric` | 实际计量指标；有效 NPU 性能为 `msprof_device_task_duration` |
| `mode`、`security_label`、`rankable` | 运行与隔离声明；当前开发结果不可用于正式排名 |

`scope: public_full` 表示该次覆盖完整公开集合，`public_and_hidden_full` 表示终评完整集合。这描述本题的测试范围：单 shape 子题的 full 集合不是原完整算子的全部 shape。

### 0 与 null 必须分开

| 情况 | 最终任务分 | 能说明什么 |
| --- | --- | --- |
| 已独立终评，编译/运行/正确性失败 | `0` | 该提交没有通过任务门槛；过程里仍可能达到过部分阶段 |
| `source_rejected` 类候选门槛失败 | `0` | 被判定为候选提交失败；当前源码门槛通常表现为 compile_failed + 具体错误类别 |
| 正确且完整 NPU 计量、基线有效 | 0–100 内的数值 | 该最终提交相对冻结参考的性能分 |
| 无任何终评、尚未完成、没有可提交历史 | `null` | 没有最终任务证据，不能等同于失败或继承旧 bench 分数 |
| 基础设施失败、无正式后端 | `null` | 环境/执行条件使结果无效 |
| 测量无效、正确但没有可用计时 | `null` | 可以保留已验证正确性，但没有有效性能分 |
| CPU smoke 样例通过 | `null` | 只验证编译与正确性，没有 NPU 性能成绩 |

`passed` 本身不保证有性能分。判断性能成功要同时检查完整正确性、performance 阶段、metric、逐 case 测量有效性和身份。

### `comparison`：参考和时间向量

- `baseline_identity`：参考 `baseline_id`、源码、题包、协议、引擎和环境绑定信息。部分字段由 controller 从冻结 baseline 补充，因此编译失败仍可归属同一比较条件。
- `reference_is_theoretical_optimum`：当前始终为 false。
- `timing_cases[]`：每个 case 的 `candidate_us`、`reference_us`、`speedup`、`case_score`、`measurement_valid`。
- `formula`：本次实际评分器报告的公式；没有执行评分时可以为空。
- `environment_fingerprint`：实际测量环境摘要；`bound_environment_fingerprint` 是冻结基线的环境绑定，不能冒充新测量。
- `protocol_digest`、`engine_digest`：协议和计量代码身份。
- `task_metadata`：固定 shape 子题的描述；完整题或旧记录可能没有此字段。

单场 `timing_cases[].speedup = reference_us / candidate_us`。这个加速比的分子是参考 kernel，不是实验里的 control 条件。

## 3. 评分公式与采样

当前 NPU 评分对每个公开性能 case 使用：

```text
r = max(1, t_candidate / t_reference)
case_score = 100 / (1 + log_1.5(r))
```

完整算子取其全部 15 个公开性能 shape 的等权平均；独立子题只有一个性能 case，分数不再除以 15。缺 case、重复 case、不同源码或无效测量不能拼成一个完整分数。

候选快于参考时分数上限为 100，但原始参考加速比仍保留。参考是当前已知实现，不是真实理论最优；更换参考会改变评分尺度，不能跨 baseline 直接归因于 Agent 改善。

内置协议每 shape 采集三轮，每轮预热 8 次、测量 32 次，先取各轮测量中位数，再取三轮中位数的中位数。每次调用输出仍接受独立验证。host wall time、Agent 自报耗时和其他 profiler 指标不能替代该 device duration。

## 4. `diagnostics`：能力过程记录

过程指标用于定位方法影响，不另加任务分：

| 字段 | 含义 |
| --- | --- |
| `highest_verified_stage` | 历史记录中实际达到的最高连续验证阶段 |
| `first_verified.{stage}` | 首次通过的操作序号、操作 ID、源码摘要、收到完整反馈时的累计秒数及范围 |
| `submitted_versions` | 受理记录中不同源码摘要的数量 |
| `completed_operations`、`outcomes` | 完成操作数及结果分类 |
| `all_observed_attempts_failed_compilation` | 已观察候选结果是否都编译失败，不代表未观察到的工作 |
| `contains_infrastructure_failure` | 历史中是否存在基础设施/正式后端不可用问题 |
| `trace[]` | 按受理顺序记录 action、outcome、错误类别、最高阶段、是否未修改 starter |

操作序号包括 final，不能直接当作已用反馈次数。正确性里程碑可能来自公开 check，因此不能替代终评隐藏测试。优化轨道起点已正确时，首次通过也可能来自未修改 starter。即便所有版本编译失败，也保留错误类别、提交版本数、反馈使用和未达成阶段，但不按代码长度或错误数量构造虚假进步分。

## 5. 实验 `manifest.json`

服务端在首次创建实验时冻结：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`experiment_id`、`name`、`created_at` | 实验格式、身份、名称与创建时间 |
| `track`、`tasks`、`repeats`、`control` | 一种轨道、选定任务、每条件重复次数和对照条件 ID |
| `policy` | 每场预算，不是整个实验共享配额 |
| `conditions[]` | 条件 ID、标签、method_digest 和公开 runner 声明/配置摘要 |
| `task_metadata` | 每题 shape/标签、题包/协议/baseline 身份与 public_case_ids |
| `trials[]` | 全部计划 trial_id、task_id、condition_id、repeat_index、attempt_index |
| `hidden_input_policy` | 同任务同轮次条件间共享隐藏输入、轮次间变化的约定 |
| `evaluation_purpose`、`agent_identity_verified`、`security_label` | 用途、模型身份与开发模式限制 |

`repeat_index` 从 1 开始。两条件的运行顺序按轮次 A→B、B→A 交替；这是减少固定顺序影响的安排，不是随机化证明。每个 task × condition × repeat 都有独立会话和预算；不能把一个会话复用成多个“独立样本”。

修改方法指令、资源字节或 runner 配置会改变配置身份。相同配置重复调用恢复/读取原实验；显式新实验使用 `--new`。同一未完成实验不能为挑选成功结果而换条件后补记录。

## 6. `trials.json` 与 `comparison.json`

`trials.json` 是所有计划 trial 的列表，不是仅成功列表。未分配可以是 PLANNED，分配过程可为 ALLOCATING；已分配记录的 status 来自裁判会话，如 PENDING、OPEN、FINISHED、EXPIRED、CANCELLED。可含 `report`、`runner_metadata` 和 `report_path`。缺失报告不是 0 分。

`comparison.json` 的 `kind` 为 `operator_experiment_comparison`，根字段为：

| 字段 | 内容 |
| --- | --- |
| `evaluation_purpose`、`workflow_only`、`evaluation_label` | 区分流程验收与方法能力研究 |
| `experiment_id`、`name`、`track`、`control`、`repeats`、`policy` | 冻结计划的比较上下文 |
| `conditions` | 方法标签、摘要和必要公开 runner 声明 |
| `trials` | 按所有计划组合归一化的逐场结果 |
| `tasks` | 各任务按条件汇总 |
| `capabilities` | 按场景标签汇总；`overlapping_labels: true` |
| `full_tasks` | 完整算子任务列表 |
| `paired_comparisons` | 每个非对照条件相对于 control 的同任务、同轮次比较 |
| `interpretation` | 单位、分母、聚合方法及解释限制 |

归一化的 `trials[]` 保留 `final_correct`、`measurement_valid`、`task_score`、`identity`、`identity_issues`、`first_correct`、反馈、会话、报告路径和执行元数据。`final_correct` 为 true/false/null，分别表示有正确证据、候选终评失败、未知。

`classification` 的当前值包括：

- `missing_trial`：计划存在，但没有提供该 trial 记录。
- `no_final_result`：有记录，但没有有效终评操作。
- `candidate_failure`：终评候选门槛失败。
- `infrastructure_invalid`：基础设施或正式后端不可用。
- `measurement_invalid`：已正确但计量不足，或测量本身无效；CPU smoke 也可落在此类。
- `unverified_result`：无法证实正确或完整计量的其他结果。
- `passed`：最终正确、指标和逐 case 测量完整有效。

分类与 `outcome` 不是同一个字段。CPU outcome=passed 但 comparison.classification=measurement_invalid 是预期行为，表示没有 NPU 性能结果。

## 7. 配对差异与汇总规则

只配对**同一个 task_id、同一个 repeat_index**的 control 和 treatment。检查题包、轨道、预算、baseline、协议、计量引擎、环境与 manifest 绑定；性能均有效时还检查评分公式和 case 集合。不同方法的 `method_digest` 应不同，不作为必须相等的条件。

环境身份优先使用实际测量值，没有测量时可使用冻结 baseline 绑定，并通过 `identity.environment_identity_source` 标明 measured 或 baseline_binding。后者使编译失败的 0 分仍能和同条件的成功终评比较，不伪造性能。

每个 pair 含：

```text
task_id, repeat_index
control_trial_id, treatment_trial_id
comparable, reasons[]
valid_performance_pair
score_delta
case_speedups[]
geometric_mean_speedup
```

计算约定：

- `score_delta = treatment_score - control_score`，合法候选失败的 0 保留；null 不补成 0。
- 实验 `case_speedups[].speedup = control_us / treatment_us`，大于 1 表示方法条件更快。
- 两侧都最终正确且完整测量有效，才产生性能 pair；一侧失败不能获得性能加速比。
- 一个 pair 内先对匹配 case 加速比取几何均值；跨 task/repeat 的有效 pair 再取几何均值。
- `comparable` 主要描述身份可比；身份可比但一侧无性能结果时，`valid_performance_pair` 仍为 false，`reasons` 可包含结果不可用原因。

汇总列出 planned_pairs、valid_performance_pairs、unavailable_performance_pairs、incomparable_pairs，不用其他轮次的成功结果补齐缺失 pair。

正确率为 `final_correct / planned`，分母包括全部计划 trial。未知单列为 correctness_unknown，不能把未知写成已证明的失败，也不能删掉后声称 100% 成功。分数/延迟统计只使用相应有效证据，旁边必须保留 count 与 unavailable。

统计对象提供 count、mean、std、min、max 和 unavailable；加速比另有 geometric_mean。std 是样本标准差，少于两个有效观测时为 null。当前报告不自动计算显著性、置信区间或跨算子泛化结论。

能力标签会重叠：如一个 case 同属 tails 和 large_gemm，不能把它算成两个独立样本。完整算子单独放在 full_summary，子题放在 non_full_summary，不将 full 当作第 16 个独立 shape。

## 8. 参考 baseline 与可复现身份

参考文件当前为 `schema_version: 2`、`kind: op_eval_v2_reference_baseline`，主要绑定：

- `baseline_id`：对除自身 ID 之外的整个规范化 baseline 对象计算的摘要。
- `package_digest`、`source_sha256`、`protocol_digest`、`engine_digest`。
- `environment` 与 `environment_digest`，以及 public case_ids 和 weights。
- `case_medians_us`、sampling_plan、完整 correctness 和逐 capture measurements。
- `metric: msprof_device_task_duration`、`unit: us`、`reference_is_theoretical_optimum: false`。

基线验证会重新从保留的 capture 样本计算中位数，核对完整正确性、采样计划和身份。不能仅手填一组更快的时间，或把旧包/其他机器的时间重命名成当前 baseline。哈希用于发现变更与绑定来源，不认证可写裁判账号的可信性。

发布研究结果时，至少记录：代码版本、题包/协议/参考/引擎身份、设备与 CANN/驱动环境、轨道、预算、全部条件与方法摘要、重复次数、全部 trial 状态、正确率分母、有效 pair 数、最终性能及范围。CLI 版本、模型提供方、用户配置和工具权限由实验者另外记录；runner/model 声明不是身份认证。

原始日志可能含用户上下文、配置、绝对路径或凭据。公开报告应经过单独审查；只隐藏 token 不等于所有字段已经适合公开。不要发布 `.eval/launcher.json`、会话 token、裁判数据库、隐藏 seed 或未经检查的 Agent 日志。

返回 [项目首页](../README.md) · [研发对照指南](../RESEARCH_GUIDE.md) · [题包接入规范](OPERATOR_SPEC.md)
