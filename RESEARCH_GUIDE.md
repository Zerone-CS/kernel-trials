# 用算子评测器验证 Agent 方法

本指南面向知识库、Skills、规划策略、工具、模型和其他 Agent 方法的研发者。目标是把“增加这个方法后是否更容易写对、是否能在相同预算内得到更快的 kernel”变成可重复、可追溯的实验。所有候选都只交付 `kernel.asc`，裁判负责固定工程编译、正确性验证、性能测量与唯一终评。

先按 [安装与部署](docs/INSTALLATION.md) 配置裁判和启动器。本文中的路径均相对于**包含 `launch.py` 的仓库根目录**，不依赖开发者原来的机器或服务器。

## 1. 选择入口

在仓库中打开已配置好的 Agent，输入以下任一命令：

| 目的 | 命令 |
| --- | --- |
| 当前 Agent 从零实现完整算子 | `/op-eval` |
| 当前 Agent 优化完整算子的正确基线 | `/op-eval optimize` |
| 当前 Agent 从零实现 Case8 | `/op-eval case08` |
| 当前 Agent 优化 Case8 | `/op-eval case08 optimize` |
| 查看服务实际安装的任务 | `/op-eval tasks` |
| 按配置做方法对照和重复运行 | `/op-eval compare experiments/knowledge_ab.json` |

Codex 使用 `$op-eval`，其余参数相同。入口文件和各 CLI 配置见 [Agent 接入](docs/AGENT_INTEGRATION.md)。这些入口属于 Agent 的命令/技能，不是在 shell 中直接输入的程序；需要排查时，等价的驱动命令是 `python3 launch.py ...`。

**单场使用当前会话，compare 使用配置指定的新 CLI。** 在 Codex 窗口里运行 `runner.provider="claude"` 的 compare，实际被测的是新启动的 Claude CLI。当前对话的知识、提示词或已有代码不会自动成为新 trial 的方法输入。通过方法配置显式提供实验材料。

两条轨道都带有固定工程；从零轨道从未实现的 kernel 开始，优化轨道从正确 starter 开始。starter 与评分参考 kernel 是不同对象。使用 `/op-eval` 恢复已分配的工作区时，其题目和轨道已经固定。

## 2. 选择场景和规模

| 选择器 | 含义 |
| --- | --- |
| `full` | 完整算子输入域、15 个公开性能 shape 与原终评边界输入 |
| `case01`–`case15` | 一个固定 shape、dtype 与转置组合；独立预算、正确性测试和分数 |
| `group:tails` | 展开 case02、case03、case06、case08，各自独立运行 |
| `group:large_gemm` | 展开 case08–case12，各自独立运行 |
| `all` | 15 个固定 shape 子任务，不包含 full |
| `all,full` | 15 个子任务加完整算子，报告分开展示 |

`tasks` 可以是选择器字符串，也可以是字符串列表；逗号分隔也可用，展开后按首次出现去重。当前 `caseNN` 目录和能力组专用于内置 BatchMatmulMaxSum 题目，不是任意新算子的自动拆题机制。全部标签、精确形状和子题测试范围见 [任务目录](TASK_CATALOG.md)。

先选与方法假设有关的少量场景。例如，知识主要讲尾块处理，可以先比较 `group:tails`；之后再用其他任务和 full 检查收益是否局限于这些形状。Case8 同时属于 tails 和 large_gemm，同一次运行不能据此算成两个独立样本。固定 shape 的收益也不证明未见 shape 泛化，更不能从同时变化的 shape、dtype 和转置属性中独立归因某项能力。

完整运行数为：

```text
去重后的任务数 × 条件数 × repeats
```

自带 `knowledge_ab.json` 选择 3 个任务、2 个条件、3 轮，合计 **18 场**，每场默认最多 20 次反馈和 60 分钟任务时间。它不是几分钟的流程检查。首次使用可从 1 个任务、2 个条件、1 轮、小预算开始；此时只能做小规模观察，不能据此宣称显著提升。

## 3. 最小对照实验

创建 `experiments/my_method.json`：

```json
{
  "schema_version": 1,
  "name": "case08-planning-pilot",
  "track": "from_scratch",
  "tasks": ["case08"],
  "repeats": 1,
  "control": "baseline",
  "runner": {"provider": "claude"},
  "policy": {"max_feedback": 4, "active_seconds": 300},
  "conditions": [
    {"id": "baseline", "label": "原始 Agent"},
    {
      "id": "planned",
      "label": "显式优化假设",
      "instructions": "每轮修改前说明当前瓶颈假设、预计影响和验证方式。收到反馈后比较预期与结果，再决定下一轮修改。"
    }
  ]
}
```

输入：

```text
/op-eval compare experiments/my_method.json
```

驱动会完成会话准备、方法安装、逐场新进程运行、裁判收尾和差异报告。示例的 4 次/300 秒是试运行预算，会影响实际成绩；正式对照时在运行前统一选择预算。若省略 `policy`，使用管理员绑定的上限，通常是 20 次/3600 秒。

`control: "baseline"` 指**对照方法条件**；评分参考 `tbest` 是管理员冻结的已知参考实现。不要把两者混为同一个 baseline。

## 4. 配置字段

配置严格拒绝未知字段。建议总是显式填写 `track`、`tasks`、`repeats`、`control`、`runner.provider` 和预算，便于审阅。

| 顶层字段 | 默认与约束 |
| --- | --- |
| `schema_version` | 当前仅支持 `1`，可省略 |
| `name` | 默认配置文件名去掉扩展名；1–200 个字符 |
| `track` | `from_scratch` 或 `optimize`；默认前者，一个实验只有一条轨道 |
| `tasks` | 默认 `full`；展开后的任务必须已由管理员安装 |
| `repeats` | 每个任务、每个条件的独立重复次数，整数 1–100；默认 3 |
| `control` | 必须等于某个条件的 `id`；默认第一个条件 |
| `conditions` | 必填，1–8 个条件；展开后全实验最多 1000 个 trial |
| `runner` | 共用 CLI 配置；未提供时使用 `{"provider":"claude"}` |
| `policy` | 仅支持 `max_feedback`、`active_seconds` 两个正整数；只能降低管理员上限 |

| 条件字段 | 作用 |
| --- | --- |
| `id` | 必填且唯一；1–80 个字符，以字母或数字开始，其余可用字母、数字、`_`、`.`、`-` |
| `label` | 展示名称，建议使用简短文本；默认等于 `id` |
| `instructions` | 方法指令文本，默认空 |
| `instructions_file` | 方法指令文件；与 `instructions` 同时提供时，追加文件文本 |
| `resources` | 文件或目录路径列表，默认空 |
| `runner` | 覆盖共用 runner 的同名字段；这是浅合并，例如条件的 `env` 会替换整个共用 `env` |

`repeats` 是新的 Agent 运行次数，不是同一个 kernel 的 profiler 采样次数。`final_grace_seconds` 由管理员策略决定，不能放在实验 `policy` 中自行修改。

## 5. 注入知识库、Skills 或工具方法

例如目录为：

```text
experiments/
  knowledge_test.json
  method.md
  knowledge/
    tiling.md
    tails.md
```

方法条件写为：

```json
{
  "id": "knowledge",
  "label": "Agent + 知识库",
  "instructions_file": "method.md",
  "resources": ["knowledge"],
  "instructions": "结合 method/knowledge/ 中的资料完成算子开发，并自行判断每条建议是否适用。"
}
```

所有文件路径相对于**配置 JSON 所在目录**。驱动把合并后的方法说明安装为 `METHOD.md`，把资源复制到 `method/`。目录会保留自己的末级目录名：资源 `knowledge` 对应 `method/knowledge/...`；单文件则使用其文件名。资源文件不能重名碰撞，不能是符号链接，最多 1000 个文件、合计 16 MiB。

指令文本和资源内容参与 `method_digest`。运行中更改方法文件会被拒绝；在下一次实验前修改它们会产生新的计划身份。资源只是被提供给 Agent：系统不会自动安装文件里的 Skill、执行安装脚本、配置 MCP 服务或建立检索索引。若方法需要外部工具，应在试验前完成受控配置，在 runner 或方法说明中明确使用方式，并记录工具版本与数据快照。

方法资源不得包含隐藏测试答案、参考输出或通过修改裁判绕开计算的指令。已公开的参考代码和历史轨迹也可能造成结果泄漏：若把这些作为知识方法输入，应明确标注任务相关知识可见性，不能把所得结果解释为无答案泄漏条件下的从零实现能力。详细约定见 [实验规范](docs/EXPERIMENT_SPEC.md)。

## 6. Runner 配置

内置 provider 为 `claude`、`codex`、`opencode`、`gemini`；其他 CLI 使用 `command`。各 CLI 的安装、认证、可用模型和配置目录必须先准备好，详见 [Agent 接入](docs/AGENT_INTEGRATION.md)。CLI 的 provider 名不等于认证过的底层模型身份。

| 字段 | 作用 |
| --- | --- |
| `provider` | 内置名称或 `command`；建议始终显式填写 |
| `model` | 可选模型标识；省略时由 CLI 当前配置决定 |
| `command` | 可选非空 argv 数组；存在时覆盖内置命令构造；`provider="command"` 时必填 |
| `env` | 子进程环境变量覆盖，字符串到字符串的对象；不要在可提交配置中填写 API 密钥 |
| `permission_mode` | 仅 Claude：`dontAsk`（默认）、`acceptEdits`、`default`、`bypassPermissions`；root 下拒绝最后一种 |
| `allowed_tools` | 仅 Claude：非空工具权限规则列表；默认 Read、Glob、Grep、Edit、Write、`Bash(./eval *)` |

显式设置模型的例子：

```json
{"provider": "claude", "model": "YOUR_CONFIGURED_MODEL_ID"}
```

替换为 CLI 真正支持的模型标识，两个方法条件保持一致。自定义 CLI 例子：

```json
{
  "provider": "command",
  "command": ["my-agent", "run", "--prompt", "{prompt}", "--workspace", "{workspace}"]
}
```

`my-agent` 和参数仅说明接口形式，必须替换为目标 CLI 的真实命令。驱动不经过 shell，只替换 `{prompt}` 和 `{workspace}`，不会展开 `~`、`$VAR`、管道或重定向。命令在 trial 工作区运行；相对可执行路径也以该工作区为基准。工具须位于执行机器的 PATH 或使用正确的绝对路径。

内置适配每场启动新会话；自定义 command 的作者负责保证不恢复上次上下文、不读取兄弟 trial。独立进程并不自动清除用户级记忆、缓存、用户指令和外部工具状态。改变模型、权限、工具或 CLI 配置与改变方法同时发生时，结论只能针对整体组合。

Claude 的配置目录按显式 `runner.env.CLAUDE_CONFIG_DIR`、执行环境继承值、CLI 默认目录的顺序确定。需要自己的配置时，可在启动 Agent 前设置 `CLAUDE_CONFIG_DIR`，或在私有 runner 配置中提供该变量；远程 compare 继承的是服务器进程环境，不会把本机全部环境变量自动上传。不要把认证目录里的文件当作方法资源复制到 trial。

## 7. 自动流程与预算

1. 读取配置、方法和 runner，计算摘要；服务端冻结题包、协议、基线、轨道、预算和全部计划 trial。
2. 每个 trial 分配独立 session 和工作区。奇数轮按条件顺序运行，偶数轮反转；这不是随机化证明。
3. 启动新的 CLI。Agent 首先执行 `./eval start` 获取资源并开启时钟，再读题、读取方法、修改 kernel 和调用 `check/bench`。
4. 反馈次数和任务时间任一先耗尽后，不再接收新版本；编译失败也消耗已受理次数。终评只能使用允许的历史源码，并且每场只有一次。
5. CLI 退出后，驱动让裁判收尾：等待已受理的在途工作，再选择历史源码终评；没有任何提交则保留 `no_final_result`，不会编造 0 分。
6. 每场结束更新报告，继续下一场，最后给出完整实验报告路径。

同任务、同轮次的不同条件共享隐藏输入种子；不同轮次使用不同种子。种子不对候选公开。公开性能输入仍遵守任务的固定协议。

默认 20 次/60 分钟是每场的上限，允许提前终评。除任务时间外，分配、终评和进程收尾仍有开销；不能把 `任务数 × 条件数 × repeats × active_seconds` 当作整组实验的精确墙钟耗时。停止和历史版本规则详见 [评测规则](EVALUATION_POLICY.md)。

## 8. 恢复与再次运行

重复执行同一 compare 命令会恢复原实验；已经完成时返回原报告。明确想再做一组独立实验时，在上一组完成后使用：

```text
/op-eval compare experiments/my_method.json --new
```

不要用 `--new` 解决网络错误或不理想的结果。改变配置或方法内容会产生新的计划身份，应保留旧实验并说明变更。

同一个驱动仍在运行时，重复调用会拒绝并发。若驱动异常中断后旧 CLI 是否存活不明，系统会拒绝再次启动，需先确认服务器进程状态；不要删除 `.eval` 或 `started.json` 来强行恢复。SSH 断开或本机 Ctrl-C 只证明本机连接结束，不证明远端 Agent 已停止。具体排查见 [故障处理](docs/TROUBLESHOOTING.md)。

## 9. 查找并解释报告

直接执行时，实验目录位于仓库根目录的父目录：

```text
op_eval_experiment_runs/<experiment_id>/
  manifest.json                  冻结的实验计划与身份
  REPORT.md                      人可读汇总
  comparison.json                配对和统计
  trials.json                    所有计划 trial 与裁判结果
  trials/<trial_id>/
    execution.json               CLI 退出状态与执行元数据
    report.json                  裁判单场报告（取得终评后）
    REPORT.md                    单场报告
    logs/                        私有 CLI 日志
    workspaces/<track>_<session>/ 当前 trial 工程与 kernel.asc
```

从配置了 `experiment_remote` 的本机启动时，驱动会上传方法快照，在服务器执行并取回四份汇总文件，返回的本机 `report_path` 位于 `.eval/remote-experiments/<experiment_id>/REPORT.md`。完整工作区和私有日志仍在服务器；返回值另列服务器路径。远程部署见 [安装与部署](docs/INSTALLATION.md)。

按以下顺序看结果：

1. **完整性和正确性**：最终正确数除以全部计划 trial 数，同时看候选失败数与未知数。3 次中只完成 1 次，不会显示为 100% 成功。
2. **任务分**：终评编译、运行、正确性失败为 0；无终评、基础设施故障或无有效性能测量为 null。正确但计时无效仍可记录“已验证正确”。
3. **同轮配对**：只比较同任务、同轮次、身份一致的条件。加速比为 control 延迟 / method 延迟，仅双方最终正确且性能有效时计算；分差为 method 分数 − control 分数，合法失败的 0 分保留。
4. **重复波动和过程**：查看有效 pair 数、缺失原因、均值、范围和样本标准差；少于两份数据时标准差为空。首次正确可能只覆盖公开测试，不能替代终评。

`tbest` 只是当前已知参考，不是理论最优。分数在快于参考时封顶，因此还要看未截断的延迟和加速比。报告只作描述性统计，不自动产生显著性结论。字段定义见 [报告规范](docs/REPORT_SPEC.md)。

## 10. 发布或分享实验

保留整个计划及其失败、未知结果，报告 task/package/protocol/baseline 身份、模型和方法声明、预算、独立重复次数、运行顺序、环境和数据可见性。不要仅选择最快一次，或用新的实验成功结果补齐旧实验失败位置。通过新任务、新轮次复验在开发任务上调好的方法，避免一边看测试结果一边修改方法却仍称其为独立验证。

发布前按 [实验规范](docs/EXPERIMENT_SPEC.md) 填写披露清单。原始日志、方法资源和配置可能含凭据、绝对路径或私有内容；公开文件需逐项审阅。当前开发模式没有账号/OS 隔离，也不认证模型身份，不能把结果宣传为正式防作弊排名。安全边界见 [安全说明](SECURITY.md)。
