# BatchMatmulMaxSum：Ascend C 算子实现与优化

交付物只有 `kernel.asc`。本文件定义算子题目和 ABI；评测命令、预算、终止规则由外部评测框架提供，不属于本题工程。

两种轨道使用同一接口：`from_scratch` 从未实现入口开始；`optimize` 从通用动态分块正确实现开始。优化起点不是用于对照的最终参考版本。可以在 `kernel.asc` 中实现 Host 参数校验、tiling、工作区管理与 Device kernel；张量数学运算应在 NPU Device 完成。只允许提交该文件，不得替换固定 harness、golden、测试或构建文件。不要添加 `main` 或重复定义载体已提供的 `TensorInfo` / `TensorGroupInfo`。

本包从既有工程迁移，保留已知最佳参考源码作为 baseline；它不是理论最优。此版本没有接入 v2 的受信 NPU 设备计时后端，也没有隔离任意 Host C++。因此正式评分关闭，Host 时长不能替代 `msprof_device_task_duration`。

## 数学语义

每个 batch 独立计算：

    A[b,m,n] = sum_{k=0..K-1} X1[b,m,k] * X2[b,k,n]
    R[b,m]   = max_{n=0..N-1} A[b,m,n]
    y[b]     = sum_{m=0..M-1} R[b,m]

逻辑输入形状固定为 X1=[B,M,K]、X2=[B,K,N]；输出是连续的 FP32 [B]，每个 batch 一个标量。只配对相同 batch，不支持 batch 广播或跨 batch 笛卡尔积。先完整计算 K 维点积，再沿 N 取最大值，最后沿 M 求和，不可交换 Max 与 Sum。

所有真实 M/N/K 元素都参与计算。输入不提供 padding、有效长度或 mask；不在算子中执行 L2 归一化。允许负输入和全负相似度，因此 Max 的初值须为负无穷或首个有效元素，不能初始化为 0。补齐元素不得错误参与有效范围的 Max。K 维累加和两级归约须采用 FP32 累加或满足下述精度的等效实现。

## 存储布局、类型与合法输入域

两个输入都是连续 ND、三维 Tensor，类型相同，只支持 FP16 或 BF16。transposeX1/transposeX2 只声明物理存储形状，不改变上述数学语义，也不意味着应另外启动一个转置算子。

| 输入 | 属性值 | 物理 shape | 逻辑元素的连续存储偏移 |
|---|---|---|---|
| X1 | transposeX1=false | [B,M,K] | (b*M+m)*K+k |
| X1 | transposeX1=true | [B,K,M] | (b*K+k)*M+m |
| X2 | transposeX2=false | [B,K,N] | (b*K+k)*N+n |
| X2 | transposeX2=true | [B,N,K] | (b*N+n)*K+k |

四种属性组合都需要支持。输入不可修改，包括其有效数据与分配边界。输出不依赖预先存在的内存内容，必须完整写入 B 个 FP32 值。

合法形状约束：

    1 <= B <= 64
    1 <= M <= 8192
    1 <= N <= 8192
    32 <= K <= 8192，且 K 是 8 的整数倍
    B*M*K <= 2^26
    B*N*K <= 2^26

两个输入的 B、K 分别相等。不支持空 Tensor、非连续 Tensor、不同输入类型或形状不匹配输入。有效输入不含 NaN、正负 Inf；输入元素可以为负数。M、N 不保证是 16 的倍数，必须正确处理尾块。上述范围是算子输入域，下面 15 个公开形状不是完整输入域。

## 固定公共 ABI

评测载体在包含 kernel.asc 前已定义：

    struct TensorInfo {
        const int64_t* shape;
        int64_t numDims;
        int32_t dtype;
    };
    struct TensorGroupInfo {
        const TensorInfo* tensors;
        int64_t numTensors;
    };

必须实现并保持下面入口的名称、参数顺序、类型及 C linkage：

    extern "C" void run_kernel(
        GM_ADDR x1, const TensorGroupInfo &info_x1,
        GM_ADDR x2, const TensorGroupInfo &info_x2,
        GM_ADDR y, const TensorGroupInfo &info_y,
        int64_t availableCoreNum, aclrtStream stream,
        bool transposeX1, bool transposeX2);

- x1/x2/y 是 Device 地址；每个 TensorGroupInfo 含一个 TensorInfo。
- 输入 numDims=3，shape 为上表中的物理 shape；输出 numDims=1，shape=[B]。
- dtype 编码：0=FP32，1=FP16，2=BF16。因此两个输入为 1 或 2，输出为 0。
- availableCoreNum 是评测环境提供的可用资源参数，不是要求每个输入都启动该数量的 block，也不能假定永远是 20。
- 在传入的 stream 上完成该次调用的计算与输出。评测载体在调用后同步该 stream；异步提交必须满足这一完成语义，内部工作区不能提前释放。
- 当前编译目标由评测环境固定为 dav-2201，使用已安装的 CANN/Ascend C 工具链。不要在提交中改写编译环境。

当前原型的性能关联支持一次公共调用对应一次计算 kernel；同一 kernel 内使用多个物理 AIC/AIV、多个 block 或调用普通 Device 辅助函数不属于多次 launch。多计算 kernel 的实现可能无法取得本原型的有效性能结果；这与数值是否正确是两个独立判定。

## 正确性判定

参考实现先读取实际量化并存储的 FP16/BF16 输入值，在 FP64 下计算点积、Max(N)、Sum(M)，最后转换为 FP32 golden。不要用量化前的随机实数或另一套布局解释来推测参考值。

每个输出元素必须有限，输出形状必须恰好为 [B]，并满足：

    abs(actual - golden) <= 1e-4 + 1e-4 * abs(golden)

所有元素全部通过才算该输入通过。重复调用逐次按上述容差校验，并检查输入不变、输出完整写入以及分配边界；FP32 输出逐位一致性仅作诊断，不是额外的正确性门槛。公开调试套件含随机、全负、零和有限幅度抵消输入，以及小形状、尾块、多 batch、多布局和连续重复调用。通过有限测试不是对整个合法域的数学证明。

## 15 个公开性能形状

tx1/tx2 分别表示 transposeX1/transposeX2，0=false，1=true。

| 编号 | B | M | N | K | 输入类型 | tx1 | tx2 |
|---|---:|---:|---:|---:|---|---:|---:|
| 1 | 1 | 1 | 1 | 32 | FP16 | 0 | 0 |
| 2 | 1 | 3 | 5 | 40 | BF16 | 1 | 0 |
| 3 | 2 | 7 | 13 | 48 | FP16 | 0 | 1 |
| 4 | 4 | 8 | 16 | 128 | BF16 | 1 | 1 |
| 5 | 8 | 16 | 32 | 128 | FP16 | 0 | 0 |
| 6 | 13 | 23 | 73 | 192 | BF16 | 0 | 1 |
| 7 | 16 | 32 | 128 | 256 | FP16 | 1 | 0 |
| 8 | 1 | 1025 | 1031 | 1032 | BF16 | 1 | 1 |
| 9 | 1 | 2048 | 1536 | 1280 | FP16 | 0 | 1 |
| 10 | 1 | 4096 | 1280 | 1152 | BF16 | 0 | 0 |
| 11 | 1 | 1536 | 2048 | 2048 | FP16 | 1 | 1 |
| 12 | 1 | 1280 | 4096 | 1536 | BF16 | 1 | 0 |
| 13 | 1 | 8192 | 64 | 128 | FP16 | 0 | 0 |
| 14 | 1 | 64 | 8192 | 128 | BF16 | 0 | 1 |
| 15 | 1 | 64 | 64 | 8192 | FP16 | 1 | 1 |

可以按真实形状、布局、对齐和硬件容量设计泛化算法或 tiling。不要根据公开测试编号、固定数据生成规律或完整测试清单硬编码答案与跳算路径。

