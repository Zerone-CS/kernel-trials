# 故障排查与恢复

先保留原会话和错误。候选编译失败、正确性失败是评测结果；连接、SDK、worker 或计量协议失败可能是基础设施问题，两者不能混成 Agent 的 0 分。字段语义见 [报告规范](REPORT_SPEC.md)。

所有命令均从指定目录执行：`launch.py` 在项目根目录；`./eval` 在返回的单场工作区。不要为了排错删除 `.eval/`、状态库、旧 trial 或重新分配预算。

## 1. 入口、连接与安装

| 现象 | 先检查 | 正确处理 |
| --- | --- | --- |
| Agent 中没有 `/op-eval` | 是否在项目根目录，项目配置是否被信任/启用，是否有同名个人命令 | Codex 用 `$op-eval`；按 [Agent 接入](AGENT_INTEGRATION.md) 核对文件，必要时刷新入口或重启 Agent |
| `LAUNCH_NOT_CONFIGURED` | 当前项目有无管理员生成的 `.eval/launcher.json` | 由管理员完成一次部署或安全交付配置；clone 不含 token |
| `INVALID_LAUNCH_CONFIG` | launcher ID、endpoint、本机转发端口是否匹配，SSH host 是否为有效别名 | 修复私有配置；不要生成假的 token 或修改服务器额度 |
| `JUDGE_UNAVAILABLE` | 服务是否存活、端口是否一致、SSH 是否可非交互登录 | 恢复原服务/转发后重试原命令；若错误来自服务内部，让管理员看服务日志 |
| `AUTH_FAILED` | token 是否与 launcher/session 对应，是否复制了错误部署的配置 | 由管理员确认原凭据；不要把 token 粘到 Issue |
| `launcher already configured` | `.eval/launcher.json` 已存在 | 保留既有历史。新题目/新部署使用独立项目配置，不通过删文件强行重绑 |
| 入口安装报手工编辑冲突 | 入口是否与其管理清单记录不同 | 先保存并审阅手工改动，再合并模板；不要删除所有恢复记录 |
| single task 未安装 | `python3 launch.py tasks` 是否列出该 case | 管理员运行相应 `configure-tasks` 并完成标定；不会静默回退 full |

查看目录和历史不会消耗反馈：

```sh
python3 launch.py tasks
python3 launch.py --history
```

`--history` 是父启动器的单场历史，不是所有实验的汇总；compare 的完整计划和全部 trial 在该实验的 `manifest.json`、`trials.json` 中。

## 2. GPU/NPU 与构建问题

| 现象 | 解释与处理 |
| --- | --- |
| `CANN SDK is not installed...` | 检查管理员安装和 `ASCEND_HOME_PATH`。A100/CUDA 不能运行当前 Ascend C 题包；CPU 样例也不能替代其 NPU 分数 |
| `Required CANN build/profile tool is missing` | 初始化 SDK 后必须能找到 `cmake`、`bisheng`、`msprof`；按 SDK 安装说明修复实际部署 |
| 缺少 `numpy` / `ml_dtypes` | 在运行裁判/管理命令的 Python 环境安装 `requirements-npu.txt`，SSH 的 `python3` 也必须使用该环境 |
| `NPU reference must be calibrated...` | 新环境尚无该题包参考标定；先 `admin.py calibrate`，子题用 `configure-tasks --calibrate-missing` |
| 基线身份不匹配 | 题包、协议、参考、计量引擎或环境改变；重新标定并建立匹配的新实验，不手改历史哈希 |
| 设备资源忙 | 已有会话取得该设备资源；等待原场结束或由管理员调查原进程。不要删资源记录并发测量 |
| `compile_failed` | 候选 kernel 未通过固定工程编译；保留日志、扣除反馈，Agent 可在剩余预算内修复 |
| `correctness_failed` | 候选输出不满足题目精度或输入完整性要求；其终评分为 0，不继续性能评分 |
| `measurement_invalid` | msprof 数据不足、不支持的任务关联、无有效设备计时等；不能用 Host 计时或模型自报值替代 |

当前 NPU 协议限制每次算子调用对应一个 device compute task。候选产生任意多个 kernel 时，不能直接把其中某一条 msprof 记录当作总时间。测量不成立应记录缺失/无效，不应伪造低延迟，也不把所有计量限制解释成算子数学错误。

## 3. 网络重试与预算停止

若 `check`、`bench` 或 `final` 的响应丢失，候选客户端会保存尚未确认的请求。恢复原请求：

```sh
./eval retry
```

不能先删除 `.eval/pending.json` 再发新请求。服务端相同 request ID 复用原受理结果，不增加反馈、不重复执行已受理操作。

在工作区读取状态和报告：

```sh
./eval status
./eval history
./eval diagnostics
./eval report
```

这些命令不消耗 `check/bench` 配额。`.eval/last.json` 保存最近一次完整反馈；默认终端只精简 profiler 样本，必要时在命令上加 `--full`。

额度或任务时钟耗尽后，不能上传新 kernel 来补考。仍可在终评选择窗口内用历史提交终评：

```sh
./eval final
```

需要选择已提交的具体版本时使用 `./eval final --source SHA256`。`final --current` 是预算尚未耗尽时提交当前文件的提前终评，不是超时后补交通道。终评已经在运行时等原操作；终评结束后只读报告，不重复运行。

如果所有版本都编译失败，报告保留编译阶段、尝试数量和错误轨迹，终评分为 0。若从未提交，报告是 `no_final_result`，分数为 null。基础设施失败与候选失败的区别由裁判报告说明，不通过 CLI 退出码推测。

## 4. compare 和远程实验

| 现象 | 处理 |
| --- | --- |
| `Agent executable is not installed or executable` | 在实际运行 compare 的机器检查所选 CLI、PATH 和认证；本机装了 CLI 不代表服务器也有 |
| CLI 拒绝参数或权限 | 对照实际 CLI 版本与适配器；保持失败 trial，修复环境后新建明确的实验，不替换旧失败行 |
| Claude 使用了错误配置 | 检查 runner 的 `env.CLAUDE_CONFIG_DIR` 和外层继承变量；远程路径必须在服务器有效 |
| `This experiment is already running` | 原驱动持有锁。等待/观察原运行，不另启同一配置或 `--new` |
| 相同命令立即返回旧报告 | 正常幂等行为。明确想做另一组且原组已结束时使用 `--new` |
| 方法资源摘要不一致 | 运行后文件被改动。不能覆盖旧快照；保留原实验，用明确的新配置身份开始下一组 |
| `Trial process outcome is unknown...` | 驱动中断后无法证明旧 CLI 已停；检查返回的 `started.json` 所指原进程和私有日志，不能直接删标记再起 Agent |
| SSH 错误返回私有 stderr 路径 | 在实际执行机器查看该文件；仅公开脱敏错误分类与必要上下文，不上传整份日志 |
| 本机 Ctrl-C 后服务器仍有进程 | Ctrl-C 终止 SSH 不证明远端工作已经停止。管理员核实原进程，不能把连接断开当作取消成功 |

普通恢复使用同一条命令，省略 `--new`：

```text
/op-eval compare experiments/my_method.json
```

驱动不会把前次中断偷偷变成新的独立尝试。若能确定进程结果，会从原记录继续收尾；若不明确，则拒绝重复启动并要求检查。未知进程状态的处理目前不是全自动恢复协议。

compare 的方法资源有总文件数和大小限制，拒绝符号链接、冲突文件名和运行后被修改的快照。遇到限制时整理输入，在新实验配置中提供必要材料；不要修改已冻结的 manifest 来让旧运行“接受”新内容。

## 5. 状态库与服务恢复

| 现象 | 处理 |
| --- | --- |
| SQLite 位于 NFS 被拒绝，或旧版本发生锁等待 | 把权威数据库部署到节点本地、仅拥有者可访问的专用目录；源码和大体积运行材料可以在共享存储 |
| `Judge database is already bound to a different location` | state/database.json 已绑定原数据库；先核实是否启动错部署，不能随意改绑定绕过 |
| `Authoritative database is missing` | 恢复匹配备份；不要让服务新建空库来“恢复”已有评测 |
| 服务重启后操作长期 RESERVED | 检查原 worker/NPU 是否仍运行；确认终止后按安装文档使用 `admin.py recover` 标记基础设施失败 |
| 服务退出后 Agent 仍等待 | 先恢复同一服务/数据库和 endpoint，再重试原请求；不能换一个空服务接受旧工作区 |

不要公开数据库、连接文件、隐藏种子、完整 state 或原始模型日志。提交 Issue 时提供：仓库版本、操作系统、Python/CLI/CANN 版本、任务与轨道、脱敏错误码、是否在本机/服务器执行、可公开的最小复现步骤。公开报告还需审查绝对路径、机器名与方法资源是否适合披露。
