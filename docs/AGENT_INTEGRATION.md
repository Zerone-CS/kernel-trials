# Agent 入口与自动实验

项目将同一工作流适配到不同 Agent 的项目级入口。管理员完成 [一次部署](INSTALLATION.md) 后，使用者无需手工建会话、挑选 session ID 或复制工程。入口文件是流程指令；真正的预算、编译、验证和评分由裁判服务执行。

## 1. 在项目里打开 Agent

从包含 `launch.py` 的仓库根目录启动 Agent，使用下表入口：

| Agent | 项目文件 | 调用示例 |
| --- | --- | --- |
| Claude Code | `.claude/skills/op-eval/SKILL.md` | `/op-eval case08 optimize` |
| Codex CLI / IDE 的项目 Skill | `.agents/skills/op-eval/SKILL.md` | `$op-eval case08 optimize` |
| Gemini CLI | `.gemini/commands/op-eval.toml` | `/op-eval case08 optimize` |
| OpenCode | `.opencode/commands/op-eval.md` | `/op-eval case08 optimize` |

项目采用各产品支持的本地入口格式：Claude 的项目 Skills、Codex 的 `.agents/skills` 与显式 `$` 调用、Gemini 的项目 TOML 命令，以及 OpenCode 的项目 Markdown 命令。入口机制核对于 **2026-11-17**，依据各产品官方文档：[Claude Skills](https://code.claude.com/docs/en/skills)、[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Gemini 自定义命令](https://geminicli.com/docs/cli/custom-commands/)、[OpenCode Commands](https://opencode.ai/docs/commands/)。

没有把 Codex 的自定义 Skill 冒充成所有版本通用的原生 `/op-eval` 命令。在 Codex CLI / IDE 中，也可以通过 `/skills` 找到 `op-eval`。如果当前产品界面使用技能选择器，从选择器显式选中这个项目的 Skill。入口未出现时，先检查工作目录、工作区信任、同名个人配置和项目文件，再按该产品方式刷新或重启。

## 2. 单条命令的含义

下表以斜杠形式展示；Codex 将开头换为 `$op-eval`。

| 输入 | 任务 |
| --- | --- |
| `/op-eval` | 当前 Agent 从零实现完整算子的 kernel |
| `/op-eval optimize` | 当前 Agent 从正确 starter 优化完整算子 |
| `/op-eval case08` | 当前 Agent 从零实现 Case8 固定 shape |
| `/op-eval case08 optimize` | 当前 Agent 优化 Case8 固定 shape |
| `/op-eval tasks` | 只查看已安装任务与能力标签，不创建会话 |
| `/op-eval compare experiments/knowledge_ab.json` | 按配置启动新 CLI 会话，完成条件对照和重复运行 |
| `/op-eval compare experiments/knowledge_ab.json --new` | 前一组结束后，明确再开一组独立实验 |

单场只接受 `full` 或一个 `case01`–`case15`。能力组和多任务选择写在 compare 配置的 `tasks` 字段中，例如 `["group:tails"]`；不要把 `/op-eval group:tails` 当作批量评测命令。配置文件路径相对于调用时的工作目录，含空格时应作为一个参数传递。

从已经分配的工作区继续单场评测，使用裸 `/op-eval` 或 `$op-eval`。此时任务和轨道已经固定，不应在工作区再指定另一题来获得新预算。

## 3. 单场流程：评测当前会话

1. 入口调用一次 `python3 launch.py ...`，服务准备或恢复该任务/轨道的工作区。
2. Agent 进入返回的绝对工作区，执行 `./eval start`。成功取得资源后才开始服务器任务时钟。
3. Agent 阅读 `TASK.md`、`OP_EVAL_WORKFLOW.md` 与固定接口，只编辑 `kernel.asc`。
4. Agent 用 `./eval check` 或 `./eval bench` 获取反馈并迭代；编译失败也消耗一次反馈。
5. Agent 用 `./eval final` 对历史提交作唯一终评；终评报告保存为 `REPORT.md` 和 `.eval/report.json`。
6. Agent 读取报告，返回真实结果和报告位置。CLI 正常退出本身不代表算子通过。

这条路径评测的就是当前 Agent。当前聊天已有上下文、个人配置、知识、MCP 或工具可能影响结果，因此正式研发对照建议创建干净会话并记录条件，或直接使用 compare。

## 4. compare：当前 Agent 负责启动，配置决定被测 Agent

compare 不让当前会话写完所有 kernel。它调用实验驱动，为每个“任务 × 方法条件 × 重复轮次”建立独立会话和工作区，再启动配置指定的 CLI。完成后，驱动从裁判取回报告并作配对汇总。

例如在 Codex 中调用 runner 为 `claude` 的实验，实际被测的是 Claude CLI；当前 Codex 仅负责启动和交付报告。在服务器远程模式下，CLI 也在服务器启动。已有聊天内容不会自动注入各 trial；方法应放在配置的 `instructions`、`instructions_file` 或 `resources` 中。

驱动默认串行运行计划，双条件按 A→B / B→A 交替轮次顺序。新进程不等于完整环境隔离：CLI 的用户级配置、外部工具和同账号文件仍可产生影响。新会话、自身不恢复旧会话及独立 kernel 起点是当前实现提供的控制；模型身份和系统隔离没有独立认证。

长时间运行时，外层 Agent 必须等待同一个驱动命令。若工具返回后台任务句柄，应使用其等待/结果机制直到结束，不用 `tail -f` 或另一个 Monitor 来监视日志，也不反复启动 compare。成功时先取得最终 JSON 中的 `report_path`、确认所有 trial 已终态，再读取报告；明确失败时报告未完成原因。相同配置重复调用只恢复或读取原实验，不能用 `--new` 作为异常恢复手段。

## 5. 配置待测 CLI

每个条件可以共用顶层 `runner`，也可以覆盖该条件的 `runner`。当前内置适配为 `claude`、`codex`、`gemini`、`opencode`，以及自定义 `command`。只提供适配不代表每个产品已完成真实 NPU 验收；已验证范围见 [VALIDATION](VALIDATION.md)。

| 字段 | 约定 |
| --- | --- |
| `provider` | 上述五种之一；常用默认值为 `claude` |
| `model` | 可选；不填使用 CLI 已配置模型，填入时传递明确模型标识 |
| `env` | 可选字符串键值对象；仅用于运行环境，不应在公共配置中写明文密钥 |
| `command` | 可选 argv 数组；替换内置命令，不能是 shell 字符串 |
| `permission_mode` | Claude 专用；默认 `dontAsk`，使用者需理解所选权限行为 |
| `allowed_tools` | Claude 专用；默认允许 Read/Glob/Grep/Edit/Write 与 `Bash(./eval *)` |

模型、省略模型时依赖的默认配置、工具权限和方法资源都属于实验条件。跨条件比较方法时应固定它们；若同时换模型与方法，结果只能解释为组合差异。

### Claude 配置目录

使用自有 Claude 配置时，在启动外层 Agent 或实验驱动前设置：

```sh
export CLAUDE_CONFIG_DIR="/path/to/your/claude-config"
```

也可在私有实验配置中设置 `runner.env.CLAUDE_CONFIG_DIR`。优先级为 runner 的显式环境配置、外层进程继承的变量；都未设置时由 Claude CLI 使用其默认目录。设置目录不会代替认证，目录中必须已有可用配置。远程 compare 使用服务器的环境，不能把本机配置目录路径直接当作服务器路径。

内置 Claude 适配以 `-p` 启动全新非交互任务，输出 JSON，并禁用会话持久化；不会 `--resume` 或继续上一 trial。它清除外层 Claude 的嵌套会话标记。root 账号下不允许设置 `bypassPermissions`，默认通过 `dontAsk` 与明确工具规则运行；如方法需要额外工具，应由实验所有者在条件中事先配置并记录，而不是由候选修改权限。[Claude CLI 参数参考](https://code.claude.com/docs/en/cli-reference)

### 其他内置适配

| Provider | 当前适配行为 |
| --- | --- |
| `codex` | `codex exec`，JSON 事件、临时会话、workspace-write、无需交互审批；实际版本必须支持适配器传递的参数 |
| `gemini` | headless prompt、JSON 输出，预设非交互权限参数 |
| `opencode` | `opencode run`，独立服务模式、自动允许未被显式拒绝的权限、JSON 输出 |

参数依据对应官方非交互接口；升级 CLI 后请先做短预算验收。[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[Gemini Headless](https://geminicli.com/docs/cli/headless/)、[OpenCode CLI](https://opencode.ai/docs/cli/commands/)

本项目的 Codex compare 适配器还保留对 `/opt/homebrew/bin/codex` 这一已知存在配置副作用的本地包装器路径的拒绝规则。它不是“所有 Homebrew 安装都不可用”的产品结论，也不影响使用当前 Codex 会话进行单场评测。遇到该保护应先核实实际可执行程序及安装来源，不要仅为绕过检查改名包装器。

### 自定义 Agent

自定义 runner 只接受 argv；`{prompt}` 和 `{workspace}` 是两个字面量占位符，例如：

```json
{
  "provider": "command",
  "command": ["my-agent", "--cwd", "{workspace}", "--prompt", "{prompt}"]
}
```

这个例子描述参数形状，不代表项目自带 `my-agent`。作者需确保该 CLI 支持相应参数、每次启动全新会话、不暗中读取其他 trial、不恢复历史 kernel，并能非交互退出。使用命令中的绝对路径时，路径必须在**实际执行机器**存在。工具不会以 shell 执行这个数组，管道、重定向和 `&&` 不会自动成为 shell 语法。

自定义命令返回 0 只能说明进程结束；可信成绩仍来自裁判终评。驱动保留 stdout/stderr 私有日志，公开报告只汇总必要元数据，不把模型自报分数作为评分证据。

## 6. 安装和更新入口文件

`admin.py configure-launcher` 会在首次部署时安装入口。仅需要刷新受管文件时，在仓库根目录执行：

```sh
python3 - <<'PY'
from pathlib import Path
from launch_routes import install_launch_routes
print(install_launch_routes(Path.cwd()))
PY
```

入口模板由 `launch_routes.py` 维护，管理清单保存在私有 `.eval/launch_routes.manifest.json`。不要分别手改四份文件；需要修改公共流程时修改模板并重新安装。安装器会拒绝静默覆盖用户已经编辑的入口。项目入口与已经分配的工作区入口承担不同职责，不能相互复制覆盖。

如果 Agent 产品不识别本项目入口，但具备 shell 和文件编辑能力，可以明确指示它调用对应的 `python3 launch.py ...` 并遵循生成的 `OP_EVAL_WORKFLOW.md`。这能使用评测后端，但不应声称该产品已经完成原生入口适配。
