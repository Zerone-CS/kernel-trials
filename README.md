# KernelTrials

**用固定预算、独立运行和可追溯报告，验证一种方法能否改善 Agent 编写与优化算子的表现。**

你可以给 Agent 增加知识库、提示词、工具或优化流程，然后比较加入方法前后的正确率、性能、反馈消耗和重复波动。Agent 只提交 `kernel.asc`；工程、测试、计时和评分由裁判管理。

当前提供 Ascend C 算子 **BatchMatmulMaxSum**（题包 ID：`batch_matmul_reduce`）的完整算子任务及 **15 个固定 shape 子任务**，另有 CPU 向量加样例用于验证安装和接口。

> 当前为研发评测。真实 NPU 正确性与性能评测已接通；尚无独立账号/OS 隔离，报告标记 `not_cheat_resistant`、`rankable=false`。固定 shape 上的提升不能直接证明未见 shape 或其他算子的泛化能力。

## 从这里开始

| 你的情况 | 下一步 |
| --- | --- |
| 已有管理员配置好的项目 | 打开 Agent，使用下面的单条命令 |
| 第一次 clone，没有 NPU | 按 [安装指南](docs/INSTALLATION.md) 的 CPU 流程验证安装 |
| 要部署自己的昇腾服务器 | 按 [安装指南](docs/INSTALLATION.md) 安装依赖、标定参考并配置启动器 |
| 要验证知识库、提示词等方法 | 阅读 [研发对照指南](RESEARCH_GUIDE.md) |
| 要加入新算子 | 阅读 [题目接入规范](docs/OPERATOR_SPEC.md) |
| 要发布当前开发目录 | 按 [GitHub 发布说明](docs/PUBLISHING.md) 白名单导出 |

“项目根目录”指包含 `launch.py` 的目录；源工作区中它可能叫 `op_eval_v2`，公开仓库可自行命名。新 clone **不包含服务器凭据、已有 launcher 或可直接复用的性能基线**，管理员需要先部署一次。

## 打开 Agent，输入一条命令

在配置好的项目根打开 Claude Code、Gemini CLI 或 OpenCode：

```text
/op-eval
```

这让当前 Agent 从空的 kernel 实现开始，自动准备固定工程、执行开发循环、提交终评并读取报告。两条轨道都提供接口和工程骨架，不要求 Agent 重建 Host 工程。

| 意图 | 命令 |
| --- | --- |
| 从零实现完整算子 | `/op-eval` |
| 从正确基线优化完整算子 | `/op-eval optimize` |
| 从零实现 Case8 | `/op-eval case08` |
| 优化 Case8 | `/op-eval case08 optimize` |
| 查看服务已安装的题目 | `/op-eval tasks` |
| 方法对照与重复运行 | `/op-eval compare experiments/knowledge_ab.json` |

**Codex 使用 `$op-eval`，其余参数相同。** 入口文件和兼容性见 [Agent 集成](docs/AGENT_INTEGRATION.md)。真实原生 CLI 验收目前覆盖 Claude Code；适配器测试通过不等于每种 CLI 都已实机验收。

**单场评测当前会话；compare 启动配置中 runner 指定的新 CLI 会话。** 示例默认使用 Claude。当前聊天中的方法不会自动转发给 trial，需写入实验条件的指令或资源。

## 最小方法对照

将以下内容保存为 `experiments/my_method.json`：

```json
{
  "schema_version": 1,
  "name": "case08-method-ab",
  "track": "optimize",
  "tasks": ["case08"],
  "repeats": 3,
  "control": "baseline",
  "runner": {"provider": "claude"},
  "policy": {"max_feedback": 20, "active_seconds": 3600},
  "conditions": [
    {"id": "baseline", "label": "原始方法"},
    {
      "id": "knowledge",
      "label": "增加知识库",
      "instructions": "结合 method/knowledge/ 中的资料完成本题，判断每条建议是否适用。",
      "resources": ["knowledge"]
    }
  ]
}
```

把资料放在配置文件旁的 `experiments/knowledge/`，然后输入：

```text
/op-eval compare experiments/my_method.json
```

这会产生 **1 个任务 × 2 个条件 × 3 轮 = 6 场独立评测**。预算按场计算；若每场用满上限，总任务时间可达 6 小时，另有终评开销。仓库的 `knowledge_ab.json` 示例是 3 个任务 × 2 条件 × 3 轮，共 **18 场**。先减少任务、轮次和每场预算验证流程，再预先确定研究配置。

每场有独立工程、方法资源、预算和源码历史。同任务、同轮次的条件共享隐藏输入种子；两条件顺序按 A→B、B→A 交替。方法说明安装为 `METHOD.md`，资源放在 `method/`。方法文件变化会改变实验身份。

相同 compare 命令再次执行会恢复或读取原实验。前一实验结束后，明确想另做一组才加 `--new`；不要用它恢复网络错误。旧进程状态不明时会拒绝重复启动，需要先检查原运行。

## 任务范围与评分

| 选择 | 范围 |
| --- | --- |
| `full` | 完整输入契约；15 个公开性能 shape，终评还有 24 个边界 shape |
| `case01`–`case15` | 各自固定 shape、dtype、转置组合；独立计分，允许合法 shape 特化 |
| `group:tails` 等 | 在实验配置中展开成若干单题，不另创组题或重复计算样本 |
| `all` / `all,full` | 15 个子题 / 15 个子题加完整算子 |

子题覆盖短点积、小矩阵布局、批量矩阵、非对齐、大矩阵、长 M、长 N、深 K。标签可以重叠，不是独立验证的能力维度，映射见 [任务目录](TASK_CATALOG.md)。新增算子可接入独立 `full` 任务；当前 15-shape 目录专用于内置算子，不会自动拆分任意新题包。

- 默认每场 **20 次 check/bench、3600 秒任务时间、1 次终评**。编译失败也消耗已受理反馈；取得资源并 start 后开始计时。预算是上限，可提前终评。
- 预算耗尽后只可选择历史提交，不能加入新源码。终评独立重编译和验证；默认历史选择并不保证选到最快版本。
- 有效终评中的编译、运行、正确性失败为 **0 分**；无终评、基础设施故障或无有效性能测量为 **null**，不补成 0。
- 单 shape 分数为 `100 / (1 + log_1.5(max(1, t_candidate / t_reference)))`。full 取 15 个 shape 的算术平均；单题只计本 shape。分数封顶 100，未截断加速比另存。
- 参考 kernel 是已知基线，**不是理论最优**；`control` 方法条件也不等于参考 kernel。

详细约束见 [评测政策](EVALUATION_POLICY.md) 和 [实验规范](docs/EXPERIMENT_SPEC.md)。

## 输出与恢复

单场工作区保存 `REPORT.md`、`.eval/report.json`。实验目录位于项目根的父目录：

```text
op_eval_experiment_runs/<experiment_id>/
  manifest.json        冻结的完整计划及身份
  REPORT.md            方法对照、正确率、差异和波动
  comparison.json      结构化统计与逐轮配对
  trials.json          全部计划 trial 及裁判结果
  trials/<trial_id>/   单场工作区、执行记录和私有日志
```

报告保留失败和未完成的 trial，仅对双方正确、计时有效且身份一致的结果计算性能加速比。报告给出有效和缺失样本数，不自动宣称统计显著性。字段见 [报告规范](docs/REPORT_SPEC.md)。

配置 `experiment_remote` 后，本机 compare 会上传方法快照、在服务器运行 CLI、取回四份汇总文件到 `.eval/remote-experiments/`。这需要管理员配置 SSH 和远端项目，**不是新 clone 自带的连接**；步骤见 [安装指南](docs/INSTALLATION.md)。

## 文档与开发

- [完整文档索引](docs/README.md)：按角色查找文档。
- [架构与数据流](docs/ARCHITECTURE.md)：启动器、实验驱动、裁判与题包的分工。
- [验证状态](docs/VALIDATION.md)：已完成测试和未验证边界。
- [故障排查](docs/TROUBLESHOOTING.md)：入口、资源、预算、基线和恢复。
- [贡献规范](CONTRIBUTING.md)、[安全报告](SECURITY.md)、[安全设计](SECURITY_DESIGN.md)。

核心回归无需 NPU 或模型 API，在 POSIX 环境的项目根执行：

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_docs.py
```

CPU 样例需要 C++ 编译器；NPU 数据生成另需 `requirements-npu.txt` 中的依赖，实际测量还需兼容的昇腾设备和 CANN 工具链。GitHub Actions 只运行 CPU/结构测试，不访问个人服务器或调用付费 Agent。

## 许可证与来源

本项目原创代码与文档采用 [MIT 许可证](LICENSE)。第三方材料保留各自的权利与许可，来源及内置算子材料的授权边界见 [THIRD_PARTY.md](THIRD_PARTY.md)。原始 rollout、赛事 PDF、旧工程归档和私有实验凭据不随公共发布包分发。
