# 来源、依赖与授权状态

本项目原创代码与文档采用 [MIT 许可证](LICENSE)。本文件记录第三方来源与分发边界；项目的 MIT 授权不替代第三方材料原有许可，也不声明已经取得额外授权。

| 内容 | 位置 | 发布处理 |
| --- | --- | --- |
| 控制器、预算、实验驱动与文档 | 根 Python、engine、docs、tests | 项目原创部分按根目录 MIT LICENSE 授权，保留版权与许可声明 |
| BatchMatmulMaxSum 题意、工程、起始和参考 kernel | problems/batch_matmul_reduce | 来自已有赛事算子工程与优化轨迹整理；保留 provenance 和源码说明。核对原始材料的再分发权，持有副本不等于已有授权 |
| CPU 向量加接口样例 | problems/vector_add_cpu_example | 项目原创示例按根目录 MIT LICENSE 授权；保留任何已有来源说明 |
| Ascend C、CANN、ACL、msprof | 用户的系统工具链、SDK include | 不打包 SDK 或二进制；使用者依其官方许可安装 |
| NumPy、ml_dtypes | requirements-npu.txt | 不复制其实现；由包管理器安装，适用各自许可。实验记录实际版本 |
| Claude Code、Codex、Gemini CLI、OpenCode | 用户安装的外部 CLI | 不打包程序、账号配置、认证数据或 API 凭据；产品名称仅用于说明适配，不代表官方认可 |
| 原始 rollout、赛事 PDF、历史压缩包、私有日志 | 开发工作区，未包含在公共导出中 | 默认不发布；额外发布需单独核对授权、个人信息和秘密 |

题包 `provenance/` 记录技术来源和哈希，不是授权证明。更改题包文件可能改变题包摘要，须重新校验并标定。

发布包须保留根目录 LICENSE。对于内置题包中的第三方材料，仍按实际授权决定是否需要独立许可、NOTICE 或调整分发范围；项目采用 MIT 不改变这些材料的原有权利。
