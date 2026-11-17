# 文档导航

所有命令均以包含 `launch.py` 的发布仓库为项目根；占位地址和目录需替换为自己的环境。新 clone 不包含部署凭据。

| 角色 / 问题 | 文档 |
| --- | --- |
| 项目是什么、如何开始 | [项目 README](../README.md) |
| 第一次安装、CPU 验收、NPU 标定、远程接入 | [INSTALLATION](INSTALLATION.md) |
| Agent 入口、当前会话与新 CLI 的区别 | [AGENT_INTEGRATION](AGENT_INTEGRATION.md) |
| 选 shape、写方法条件、知识库对照与重复运行 | [RESEARCH_GUIDE](../RESEARCH_GUIDE.md) |
| 15 个 shape 与能力场景映射 | [TASK_CATALOG](../TASK_CATALOG.md) |
| 预算、重试、停止与终评规则 | [EVALUATION_POLICY](../EVALUATION_POLICY.md) |
| 公平性、方法身份、失败保留、研究结果披露 | [EXPERIMENT_SPEC](EXPERIMENT_SPEC.md) |
| 报告字段、评分公式、0 与 null、性能配对 | [REPORT_SPEC](REPORT_SPEC.md) |
| 控制器、实验驱动、题包和 Agent 如何协作 | [ARCHITECTURE](ARCHITECTURE.md) |
| 加入新算子需要哪些文件和接口 | [OPERATOR_SPEC](OPERATOR_SPEC.md) |
| 入口不显示、编译/计时失败、资源忙、恢复 | [TROUBLESHOOTING](TROUBLESHOOTING.md) |
| 哪些功能已实测、哪些尚未验证 | [VALIDATION](VALIDATION.md) |
| 修改代码、提 PR、回归要求 | [CONTRIBUTING](../CONTRIBUTING.md) |
| 安全问题报告与现有信任边界 | [SECURITY](../SECURITY.md)、[SECURITY_DESIGN](../SECURITY_DESIGN.md) |
| GitHub 导出、数据排除、许可证与发布 | [PUBLISHING](PUBLISHING.md)、[THIRD_PARTY](../THIRD_PARTY.md) |

建议使用者先读 README 和 Agent 入口；研究者随后读研发指南、实验规范与报告规范；部署者从安装指南开始。题包契约与评测流程相互独立，贡献新题时不必阅读所有实验驱动实现，但必须提供可执行测试接口。

## 文档约定

- MUST / 必须表示本项目协议要求；SHOULD / 建议表示研发实践。提示词中的“必须”不代表操作系统已经强制隔离。
- “正确”要注明公开反馈还是最终 public+hidden 校验；“性能有效”还要求计量协议满足。
- “baseline”可能指已知参考 kernel，也可能是实验中的对照条件 ID，文档均按上下文区分。
- “重复运行”指新的 Agent 会话；“重复采样”指同一候选程序的计时 capture，两者不能相互替代。
- 文档描述当前开发实现，不把安全设计草案、未来扩展或模拟测试当成已经部署的功能。

自动检查本地链接、代码围栏、JSON 示例和实验配置：

```sh
python3 scripts/check_docs.py
```

检查不访问网络、不运行 Agent，也不能证明所有自然语言或外部文档始终有效。修改 CLI 适配器时仍须核对对应产品版本。
