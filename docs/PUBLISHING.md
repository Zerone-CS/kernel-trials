# GitHub 发布与维护规范

本仓库发布单元是评测器目录本身：公开仓库根目录应直接包含 `launch.py`、`engine/`、`problems/` 和本文档目录。研究工作区可能还保存原始 rollout、轨迹压缩包、论文 PDF、历史实现和服务器证据；这些材料不随评测器源码默认发布。

发布是一项由维护者明确执行的操作。项目工具只准备新目录，不会运行 `git init`、提交、推送、创建远程仓库或自动选择许可证。

本项目已由维护者选择 [MIT 许可证](../LICENSE)，发布时保留根目录 LICENSE。第三方来源与许可边界见 [THIRD_PARTY](../THIRD_PARTY.md)。

## 1. 从白名单导出新目录

在当前评测器源码根运行。输出路径必须尚不存在，并且位于源码树之外：

```sh
python3 scripts/export_public.py --output ../operator-eval-public --dry-run
python3 scripts/export_public.py --output ../operator-eval-public
```

`--dry-run` 做相同的来源校验和清单计算，不创建输出。缺少必需文档、入口或脚本会明确报错，不能用一个不完整导出来冒充已就绪仓库。导出不会删除或改写来源；若目标已存在，即使为空也会拒绝覆盖。要再次导出，请明确选择一个新的目录，保留或自行审查旧目录。

白名单包括：

- 明确列名的根 Python 入口、公共文档、`requirements-npu.txt` 与 `.gitignore`。
- `engine/`、`tests/`、`problems/`、`docs/`、`experiments/`、`.github/` 中符合规则的源文件。
- 四类 Agent 的固定项目入口文件；不复制它们旁边的个人设置。
- `scripts/` 中列名的文档检查、公开导出及 smoke 工具。
- 已由维护者提供的 LICENSE/NOTICE 文件；本项目包含 MIT LICENSE。工具本身也支持尚未提供许可证的来源，并会如实记录缺失状态。

目录内允许 `.py`、`.md`、`.json`、`.txt`、`.asc`、`.h`、`.cpp`、`.yml`、`.yaml`、`.toml`，以及 CMakeLists、LICENSE、NOTICE 等明确文件名。新增类型或工具应通过修改导出白名单、测试和本文档来审查，不采用“复制整个工作区再删除几个敏感文件”的流程。

默认不导出 `.eval/`、工作区、证据、数据库、缓存、日志、构建产物、个人配置、`SERVER_VALIDATION.md` 或任何未知根文件；原始 PDF、zip 和二进制也不在允许范围。选中范围内的符号链接和特殊文件会被拒绝，不跟随它们读取树外内容。

**白名单不等于内容脱敏。** 方法资料、JSON、源码注释和文档都可能含秘密或未经授权的内容。即使扩展名和位置合规，也必须人工审查。文件名过滤无法识别所有凭据。

## 2. 保持题包身份

每个已注册题包的所有文件都参与内容摘要。导出工具复制原始字节，并在清单中记录每个题包的摘要；不会清理、重写或脱敏 `problems/` 内的内容。

如果题包内存在将被过滤的日志、缓存或不允许的二进制，导出直接失败，避免悄悄漏文件导致身份变化。不要为绕过导出而清理已标定原题包。应先核对材料来源，必要时创建新的、经过审查的题包版本，重新验证和标定后再发布。

说明文件和 provenance 也参与题包摘要。如果它们包含不宜公开的信息，不能只删掉字符串后继续声称使用原 baseline；需要按题包版本变更处理。

## 3. 审查 `PUBLIC_MANIFEST.json`

导出最后生成清单：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`kind`、`layout` | 清单格式和独立仓库根布局 |
| `files[]` | 每个导出文件的相对路径、`size_bytes` 与 SHA256 |
| `problem_packages[]` | 每个题包的相对路径、文件数和完整摘要 |
| `license.present`、`license.files` | 是否存在项目许可证文件及其名称 |
| `license.notice_files` | 已提供的 NOTICE 文件 |
| `license.selected_by_exporter` | 恒为 false；导出工具不选择许可证 |
| `review_required` | 内容、来源授权和许可证仍需审查 |

清单不包含来源绝对路径、launcher 凭据或服务端 token，也不列入自身摘要，避免循环定义。清单存在是文件写出完成的标志；若 I/O 失败后只有部分目录而没有清单，视为未完成导出，不要发布。工具不会悄悄删除这个目录。

检查清单是否匹配实际输出：

```sh
cd ../operator-eval-public
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
root = Path.cwd()
manifest = json.loads((root / "PUBLIC_MANIFEST.json").read_text())
for item in manifest["files"]:
    data = (root / item["path"]).read_bytes()
    assert len(data) == item["size_bytes"], item["path"]
    assert hashlib.sha256(data).hexdigest() == item["sha256"], item["path"]
print("Verified", len(manifest["files"]), "exported files")
PY
```

校验只说明文件与清单一致，不证明许可证、内容安全或测量可信。后续编辑导出目录会使原清单不再描述当前字节；应保留其导出快照含义，或从审查完成的源码重新导出，不能把过期摘要当成新版本证据。

## 4. 发布前内容与授权审查

逐项检查：

1. 没有 API key、访问令牌、cookie、私钥、launcher/session 凭据或服务端数据库。
2. 没有用户主目录、私人服务器地址、SSH 别名、邮件、内部 URL 或未经审查的模型原始日志。
3. 实验方法资源的来源、作者和分发权限明确；示例知识不能被描述成没有依据的普适优化规则。
4. 题目描述、固定工程、参考 kernel、迁移代码和第三方材料的来源与授权已核对，详见 [第三方材料说明](../THIRD_PARTY.md)。
5. 不包含应保密的隐藏随机 seed 或当前会话答案。开源的测试构造与参考实现不再是未公开知识，应在实验协议中承认这一点。
6. README 对功能、已验证 CLI、硬件条件和开发模式限制的表述与代码一致；不把 smoke 作为方法效果实验。
7. 确认根目录 MIT LICENSE 已进入发布清单，版权与许可文本未丢失；第三方材料的原有声明按其适用许可保留。导出工具不替维护者重新选择或改写许可证。

`.gitignore` 只影响未跟踪文件，不能移除已提交历史中的秘密。如果其他仓库历史曾包含凭据，应先停止发布并处理凭据与历史，不能只补 ignore 规则后继续推送。

## 5. 在干净导出目录验证

在导出根目录运行文档检查与 CPU 回归：

```sh
python3 scripts/check_docs.py
python3 -m unittest discover -s tests -v
```

检查 README 中声明的 Python、编译器等依赖。没有 NPU 的环境可以运行不需要设备的回归，但不能因此宣称完成真实 NPU 性能验收。测试可能生成 `__pycache__`；不要把验证产物加入发布提交。

导出不携带 `.eval/launcher.json`。新使用者必须按 [部署说明](INSTALLATION.md) 配置一次裁判、连接和参考标定，之后才可在 Agent 中直接使用入口。公开仓库没有你的私人服务权限，文档不得把原机器的“已经部署”状态写成所有 clone 都可直接运行。

## 6. 维护者手动创建 Git 提交

以下操作由维护者在审查后的导出目录执行，不由导出工具自动执行：

```sh
git init -b main
git status --short --untracked-files=all
git add .
git diff --cached --stat
git diff --cached --check
git diff --cached
```

先审查完整 staged diff，再提交：

```sh
git commit -m "Publish operator evaluation toolkit"
```

远程地址由维护者提供。下面用 shell 变量交互读取，示例不绑定任何真实组织、账户或仓库：

```sh
read -r -p "GitHub repository URL: " OP_EVAL_REMOTE_URL
git remote add origin "$OP_EVAL_REMOTE_URL"
git push -u origin main
```

最后一段使用 Bash。推送前确认地址、仓库可见性和已提交内容；不使用强制推送来覆盖已有仓库历史。若使用 GitHub 网页新建空仓库，不要同时创建会与本地初始提交冲突的文件，或按正常 Git 合并流程处理。

## 7. 后续版本和结果附件

- 代码变更说明应指出改变的是入口、实验组织、题包、计量代码还是评分；题包/协议/计量变化可能要求重新标定。
- 维护者保存旧实验身份与全部 trial，避免升级覆盖历史结果。
- README 与发布说明只引用经过审查的摘要；原始 Agent 日志、运行工程和 profiler 数据另行审查后作为独立附件，不自动纳入源码仓库。
- 来源授权、许可证、依赖和已验证范围随版本更新；新增声明应能追溯到测试或实机记录。
- 报告分享遵循 [报告规范](REPORT_SPEC.md)；贡献审查遵循 [贡献指南](../CONTRIBUTING.md)。

返回 [项目首页](../README.md)
