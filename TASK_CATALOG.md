# 全量任务与固定 shape 子任务

`full` 保留原 BatchMatmulMaxSum 题包和完整输入域：15 个公开性能规格，以及原有
24 个终评边界输入。`case01` 至 `case15` 是 15 道可独立评分的固定 shape 特化任务。
每道子任务固定 B/M/N/K、dtype 和 transposeX1/transposeX2；不再要求其它规格。
两种入口都只提交 `kernel.asc`，固定工程和 ABI 由裁判提供。

| 任务 | (B,M,N,K) | dtype / 布局 | 候选能力标签 |
|---|---|---|---|
| case01 | (1,1,1,32) | FP16 NN | tiny_latency |
| case02 | (1,3,5,40) | BF16 TN | small_layout, tails |
| case03 | (2,7,13,48) | FP16 NT | small_layout, tails |
| case04 | (4,8,16,128) | BF16 TT | small_layout |
| case05 | (8,16,32,128) | FP16 NN | batched_cube |
| case06 | (13,23,73,192) | BF16 NT | batched_cube, tails |
| case07 | (16,32,128,256) | FP16 TN | batched_cube |
| case08 | (1,1025,1031,1032) | BF16 TT | tails, large_gemm |
| case09 | (1,2048,1536,1280) | FP16 NT | large_gemm |
| case10 | (1,4096,1280,1152) | BF16 NN | large_gemm |
| case11 | (1,1536,2048,2048) | FP16 TT | large_gemm |
| case12 | (1,1280,4096,1536) | BF16 TN | large_gemm |
| case13 | (1,8192,64,128) | FP16 NN | long_m |
| case14 | (1,64,8192,128) | BF16 NT | long_n |
| case15 | (1,64,64,8192) | FP16 TT | deep_k |

NN/NT/TN/TT 表示两个输入的转置属性。`tails` 表示可能涉及尾部／非 tile 对齐的场景，
不是所有维度均为奇数；例如 Case8 的 K=1032 是偶数，但仍有 tile 尾部。
Case8 同时属于大矩阵与尾部组。标签可以重叠，是挑选实验场景的入口，不是已验证的
性能瓶颈、独立能力因子，也不要求 Agent 复刻参考代码中的 Cube、Vector 或归约路径。

## 选择语义

- `full`：原完整算子任务。
- `case06`：只评 Case6 的固定规格。
- `group:tails`：展开为 case02、case03、case06、case08 四道独立题。
- `group:large_gemm`：展开为 case08 至 case12。
- `all`：仅展开 15 道子任务，不重复包含 full；需要两者时显式选择 `all,full`。
- 支持逗号分隔或字符串列表，按首次出现顺序去重。空选择默认 full。

group 只选择任务集合，没有独立题包、额外混合成绩或重新分配的组内权重。
各子任务保留自己的 session、预算、参考标定、结果与报告。对子题做同规格常量传播、
展开和 tiling 特化是允许的；硬编码输入值、种子、golden、输出答案和跳算仍不允许。
固定规格成绩描述这个场景的表现，不能宣称其证明了 shape 泛化能力。

## 子任务正确性、性能与起点

每个子题公开套件只含自己的一个随机输入规格。终评增加同一 shape、dtype 和转置组合
下的四种输入：重新采样随机、全负相似度、零、有限幅度抵消。随机终评至少重复 16 次，
其它每类至少 3 次，每次输出均独立验证；不混入父题另外 24 个边界 shape。
随机性和重复调用检查是防止固定答案与时序错误的部分手段，不构成对抗代码的安全隔离。

`from_scratch` 继承原未实现入口；`optimize` 继承通用正确动态分块 kernel。
它们都不是最终参考实现。完整已知最佳源码原样保存在裁判题包内，裁剪其测试集合，
不裁剪参考源码，也不把参考、golden 或 judge 导出给待测 Agent。

性能只测这一个公开 shape，仍使用原 msprof 设备任务时长协议（3 次独立采集，每次
8 次预热、32 次测量）。分数是本 shape 的原公式结果，不除以 15；终评正确性失败
仍受正确性门禁约束。full 的 15-shape 聚合和原包字节保持不变。

## 管理员 API 与不可变身份

```python
from pathlib import Path
from engine.task_catalog import list_tasks, resolve_tasks, materialize_task

catalog = list_tasks()  # full + 15 个子任务及形状、多标签
tasks = resolve_tasks("group:tails,case01")
package = materialize_task(
    Path("problems/batch_matmul_reduce"), "case06",
    Path("generated_problems/batch_matmul_reduce_case06"),
)
```

生成器是可信管理代码，不是提交给 Agent 执行的修改工具。每个子包是完整独立题包，
含 `problem.json` 中的 `evaluation_task` 和 `provenance/task.json` 中的父包 digest。
同参数、相同父包产生确定性字节；目的目录存在时只有完全相同才复用，否则拒绝覆盖。
子包不得写入原父包内部。`materialize_task(..., "full", ...)` 直接返回原包，不写目录。

**每个子包必须独立重新标定 baseline。** 不能从 full 的旧向量挑出一个时间冒充新标定。
包 digest、协议 digest 和性能 case 集合都绑定到 baseline；只保留 reference 源码身份。
生成包本身不执行编译、NPU、标定或评测，不能算实机验证。管理员完成新包标定和分配后，
客户端继续使用既有评测入口。开发模式仍未部署正式 Host 代码隔离，不产生正式排行榜成绩。
