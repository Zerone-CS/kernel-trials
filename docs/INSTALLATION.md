# 安装、部署与升级

本文面向部署评测器的维护者。普通实验使用者只需在已经配置好的项目中打开 Agent 并执行入口，见 [Agent 接入](AGENT_INTEGRATION.md)。以下命令均假定当前目录是含有 `launch.py` 的发布仓库根目录。

一次部署包含两部分：运行裁判服务并标定题目；给使用者的项目安装入口和私有启动器配置。`git clone` 不会携带任何有效 token，也不会自动连接维护者的服务器。

## 1. 环境要求

| 环境 | 必需组件 | 能验证什么 |
| --- | --- | --- |
| CPU 流程验收 | POSIX 系统、Python 3.9+、可执行 C++14 的 `c++` | 会话、固定工程编译、正确性、预算和报告；没有 NPU 性能分 |
| NPU 开发评测 | Linux、可独占的昇腾 NPU、匹配的驱动与 CANN / Ascend C、`cmake`、`bisheng`、`msprof`、`numpy`、`ml_dtypes` | 正确性、NPU 计时、参考相对评分 |
| 自动重复实验 | 上述裁判环境，加上已安装、已认证的待测 CLI | 驱动为每个 trial 启动新的 Agent 进程 |
| 本机连接远程裁判 | 本机 Python、SSH 客户端、可非交互登录的 SSH 配置 | 本机单场开发，以及转交服务器执行 compare |

实现使用 `fcntl` 和 POSIX 进程组；不支持直接在原生 Windows Python 上运行。WSL 没有单独验收。完整 NPU 实机记录来自 Ascend 910C、CANN 9.0.0；其他设备或 SDK 组合需要重新验证，不能只复制旧基线。支持范围见 [验证范围](VALIDATION.md)。

项目 Python 核心使用标准库。NPU 数值依赖可安装到你管理的虚拟环境：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-npu.txt
```

依赖清单不是完整环境锁文件。实验时记录实际 Python、依赖、CLI、CANN 和驱动版本。服务与管理命令应使用同一 Python 环境；SSH 远程执行使用服务器 PATH 中的 `python3`。

## 2. 先做不依赖 NPU 的流程验收

建议用专门的新 clone 运行 CPU 验收。一个项目的 `.eval/launcher.json` 绑定一个父题目；不要先把正式 NPU 项目绑定为 CPU 样例，再删除配置来切换题目。

将下面的示例 URL 换成项目实际发布地址，再运行：

```sh
git clone "https://github.com/YOUR_ORG/YOUR_REPO.git" operator-eval-cpu
cd operator-eval-cpu
python3 --version
c++ --version
```

CPU 样例无需安装 NPU 数值依赖。后面的 NPU 部署使用另一个新 clone，例如 `operator-eval-npu`；两者不共享 `.eval/launcher.json`。

### 2.1 准备独立目录

以下变量块在随后两个终端中都执行。数据库目录必须在**节点本地文件系统**，不能位于 NFS；根据你的环境调整路径。

```sh
umask 077
export OP_EVAL_PROJECT_ROOT="$PWD"
export OP_EVAL_STATE="$HOME/.local/state/operator-eval-cpu"
export OP_EVAL_DB_DIR="/var/tmp/operator-eval-${USER}-cpu-db"
export OP_EVAL_DATABASE="$OP_EVAL_DB_DIR/budget.sqlite"
mkdir -p "$OP_EVAL_STATE" "$OP_EVAL_DB_DIR"
chmod 700 "$OP_EVAL_STATE" "$OP_EVAL_DB_DIR"
```

`chmod` 只针对本例新建的专用目录，不要对共享父目录执行权限修改。如果这些目录已有另一场部署的数据，请使用新的目录名，不要覆盖或清空它们。

### 2.2 终端 A：保持裁判服务运行

```sh
python3 service.py \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --allow-unisolated-development \
  --port 8765
```

服务只监听 `127.0.0.1`。`development` 明确允许没有 OS 隔离的可信开发验收；当前 `formal` 模式不能运行候选代码。

### 2.3 终端 B：配置一次启动器

在同一 clone 根目录、同一变量配置下执行：

```sh
python3 admin.py configure-launcher \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --problem problems/vector_add_cpu_example \
  --project-root "$OP_EVAL_PROJECT_ROOT" \
  --endpoint http://127.0.0.1:8765 \
  --purpose entry_smoke \
  --max-feedback 2 \
  --seconds 120
python3 launch.py tasks
```

这一步创建启动器，安装四种 Agent 入口，将凭据保存到私有 `.eval/launcher.json`，**不会创建评测会话或启动任务时钟**。CPU 启动器的目录应只有 `full`；不能对 CPU 样例调用内置 BatchMatmulMaxSum 的 `configure-tasks`。

### 2.4 没有 Agent 时先验证后端

```sh
python3 launch.py optimize
```

复制返回 JSON 中的 `workspace` 路径并进入该目录，然后执行：

```sh
./eval start
./eval check
./eval bench
./eval final
./eval report
```

预期：固定工程编译成功、正确性通过、会话最终为 `FINISHED`；工作区生成 `REPORT.md` 和 `.eval/report.json`。CPU 样例不产生有效 NPU 分数，`score: null` 是预期行为。

### 2.5 验证原生 Agent 入口

回到已配置的项目根目录，在新 Agent 会话中输入：

```text
/op-eval optimize
```

Codex 输入 `$op-eval optimize`。由于启动器用途为 `entry_smoke`，入口要求保留正确 starter，并执行 check → bench → final。上一场已经结束后，这次明确的新调用会创建另一场验收。不要将这种用途下的结果当成 Agent 写算子或优化能力成绩。

## 3. 部署真正的 NPU 研发评测

使用一个新的正式实验 clone，并为其选择独立状态目录。以下变量同样需要在所有管理/服务终端中保持一致：

```sh
umask 077
export OP_EVAL_PROJECT_ROOT="$PWD"
export OP_EVAL_STATE="$HOME/.local/state/operator-eval-npu"
export OP_EVAL_DB_DIR="/var/tmp/operator-eval-${USER}-npu-db"
export OP_EVAL_DATABASE="$OP_EVAL_DB_DIR/budget.sqlite"
mkdir -p "$OP_EVAL_STATE" "$OP_EVAL_DB_DIR"
chmod 700 "$OP_EVAL_STATE" "$OP_EVAL_DB_DIR"
```

确认设备可以独占，CANN 已由管理员安装。若 SDK 不在默认安装目录，通过环境设置它的实际根目录：

```sh
export ASCEND_HOME_PATH="/path/to/installed/cann"
python3 -c 'import numpy, ml_dtypes; print("numeric dependencies ready")'
```

这里的 `/path/to/installed/cann` 必须替换为真实目录。后端会查找该目录或其父目录下的 `set_env.sh`，并验证初始化后能找到 `cmake`、`bisheng` 和 `msprof`。它不会安装驱动或 SDK。默认题包冻结 `dav-2201`、设备 `0`；换架构、设备或测试协议属于题包变更，按 [题目接入规范](OPERATOR_SPEC.md) 创建新版本并重新标定。

### 3.1 标定完整算子的参考实现

在裁判没有运行其他 NPU 任务时执行：

```sh
python3 admin.py calibrate \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --problem problems/batch_matmul_reduce
```

标定会真实编译参考 kernel、验证正确性并采集 msprof 数据；不是读取仓库里的历史时延。新环境必须产生自己的基线。参考实现只是当前已知 baseline，不是理论最优。

### 3.2 配置完整算子与单 shape 任务

```sh
python3 admin.py configure-launcher \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --problem problems/batch_matmul_reduce \
  --project-root "$OP_EVAL_PROJECT_ROOT" \
  --endpoint http://127.0.0.1:8766

python3 admin.py configure-tasks \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --problem problems/batch_matmul_reduce \
  --project-root "$OP_EVAL_PROJECT_ROOT" \
  --tasks all \
  --calibrate-missing
```

不传预算参数即每场最多 **20 次反馈、3600 秒任务时间**；另外有唯一终评及其选择宽限。`configure-tasks` 为 15 个子题分别生成包、标定参考、绑定独立任务。可以先用 `--tasks group:tails` 安装一个子集；`all` 不包括完整算子，后者已由父启动器提供。它是当前算子的内置拆题器，不是任意新算子的通用 shape 自动拆分器。

`configure-launcher` 若发现已有配置会拒绝覆盖；这是保留历史的行为。相同身份的 `configure-tasks` 可以重复执行；如果题包、环境或协议已经变化，不应靠覆盖旧文件维持原任务身份。

### 3.3 启动服务并检查目录

终端 A：

```sh
python3 service.py \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --allow-unisolated-development \
  --port 8766
```

终端 B：

```sh
python3 launch.py tasks
```

安装全部子题后，应列出 `full` 与 `case01`–`case15` 共 16 个任务。现在普通用户可打开项目并执行 `/op-eval case08 optimize`，或按 [研发对照指南](../RESEARCH_GUIDE.md) 配置 compare。

服务没有自动安装为系统服务。长期部署可由管理员用现有进程管理设施托管，但必须固定工作目录、Python 环境、状态与数据库路径；不能在重启时创建新数据库。设备被其他评测占用时，应等待原场结束，不删除资源状态绕过互斥。

## 4. 从本机连接远程部署

最简单的方式是在服务器仓库目录直接打开 Agent。需要本机单场编辑或本机发起 compare 时，额外准备一份同版本的本机 clone 和 SSH 配置。

### 4.1 建立 SSH 主机别名

在使用者自己的 SSH 配置中设置 `op-eval-server`，填写真实主机、账号和必要的跳板机。先验证 `ssh op-eval-server` 能成功登录；自动驱动使用 `BatchMode=yes`，不能依赖运行中输入密码。

服务仍保留 loopback 监听，可手工开启隧道：

```sh
ssh -N -L 127.0.0.1:18766:127.0.0.1:8766 op-eval-server
```

### 4.2 安全交付启动器配置

管理员将服务器已生成的 `.eval/launcher.json` 通过可信渠道复制给本机 clone，保留原 `launcher_id` 和 `token`，只修改连接配置。不要把真实凭据写入 Git、Issue 或公共示例。

下面是**字段示意，不是可直接使用的凭据文件**：

```json
{
  "launcher_id": "<administrator-issued-launcher-id>",
  "token": "<administrator-issued-token>",
  "endpoint": "http://127.0.0.1:18766",
  "ssh_forward": {
    "host": "op-eval-server",
    "local_port": 18766,
    "remote_port": 8766
  },
  "experiment_remote": {
    "ssh_host": "op-eval-server",
    "project_root": "/srv/operator-eval"
  }
}
```

`project_root` 是服务器上实际包含 `launch.py` 的绝对目录，需按部署修改。`ssh_forward` 允许启动器在连接失败时恢复本地转发；`experiment_remote` 把整个 compare 交给服务器。服务器自己的 launcher 配置不能再包含 `experiment_remote`，否则会拒绝递归转发。

本机还需安装入口。该操作只生成受管入口，不创建会话或读取凭据内容：

```sh
python3 - <<'PY'
from pathlib import Path
from launch_routes import install_launch_routes
print(install_launch_routes(Path.cwd()))
PY
chmod 600 .eval/launcher.json
python3 launch.py tasks
```

如果本机仓库已包含相同入口，安装器会保留它们并记录管理状态；若文件被手工修改导致冲突，按 [故障排查](TROUBLESHOOTING.md) 处理，不能为安装而直接删除所有 `.eval` 文件。

本机 compare 会上传配置与方法资源的不可变快照，服务器启动实际 CLI，结束后取回 `REPORT.md`、`manifest.json`、`comparison.json`、`trials.json`。完整 trial 工作区与原始 CLI 日志留在服务器。本机配置中的资源路径在本机解释；`runner.command`、模型认证和外部工具在服务器解释。

## 5. 升级、状态保留与恢复

| 数据 | 位置/性质 | 升级要求 |
| --- | --- | --- |
| 源码、题包、入口模板 | 发布仓库 | 固定版本并记录变更；题包变更产生新身份 |
| 权威预算数据库 | `OP_EVAL_DATABASE` | 保留；必须与对应 state 配套恢复 |
| 裁判私有状态 | `OP_EVAL_STATE` | 包括绑定、源码快照、隐藏种子、基线、运行证据 |
| 项目连接与恢复记录 | `.eval/` | 私有配置，保留，不随 Git checkout 重置 |
| 单场工作区 | `workspaces/` | 保留 Agent 修改和会话身份 |
| 对照实验 | 仓库父目录的 `op_eval_experiment_runs/` | 保留完整计划与所有 trial，不能仅复制成功结果 |

升级前确认没有活跃候选、正在标定的任务或未完成操作，再停止服务并备份**数据库与状态文件的匹配快照**。SQLite WAL 运行期间不能只复制主 `.sqlite` 文件；使用 SQLite backup API，或在服务停止、写入结束后进行完整备份。状态中 `database.json` 已绑定路径，随意移动数据库或删掉绑定文件可能破坏预算连续性。

源码升级后先执行项目回归。题包、参考源码、计量引擎、协议或环境身份变化时重新标定；已有实验仍绑定旧身份，不把新结果接到旧实验里。文档与入口提示的改动也应记录版本，以解释 Agent 行为差异。

如果服务丢失某个正在执行的 worker，先确认该 worker 与 NPU 工作已终止，再由管理员恢复对应 reservation：

```sh
python3 admin.py recover \
  --state "$OP_EVAL_STATE" \
  --database "$OP_EVAL_DATABASE" \
  --mode development \
  --operation-id "$OP_EVAL_OPERATION_ID" \
  --worker-terminated
```

`OP_EVAL_OPERATION_ID` 必须来自原会话记录。这会把该操作记录为基础设施失败，已消耗的反馈不退还；它不是重置预算或重跑失败候选的命令。`--worker-terminated` 是管理员对事实的确认，不能当作自动杀进程功能。

本机中断 SSH 不证明远端进程已经退出。出现未知进程状态时，检查原进程与日志后恢复同一实验；不要使用 `--new`、删除 `started.json` 或复制成功 trial 来掩盖中断。更多具体错误见 [故障排查](TROUBLESHOOTING.md)。
