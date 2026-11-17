# VectorAdd：题目包接口示例

本题仅用于验证通用评测框架和接入流程，不是昇腾性能题或正式排行榜题。

给定两个连续 FP32 向量 `x`、`z`，长度同为 `n`，计算 `y[i] = x[i] + z[i]`。输入有限，`1 <= n <= 1048576`；输出与输入不重叠，输入不可修改，必须完整写入 `n` 个 FP32 输出。

只修改 `kernel.asc`。固定接口见 `operator_contract.h`：

```cpp
void run_kernel(const float* x, const float* z, float* y, std::size_t n);
```

每个输出需满足 `abs(actual - expected) <= 1e-6 + 1e-6 * abs(expected)`。参考结果依据实际存储的 FP32 输入计算后舍入为 FP32。

公开测试长度为 32 和 4097；隐藏测试使用其他长度、不同种子生成的新数据。不要访问输入之外的测试文件、参考结果、评测器或其他提交。

`from_scratch` 起点尚未实现；`optimize` 起点是正确的简单循环。工程入口和构建过程固定，不能改写。CPU 时间仅用于框架冒烟测试，不会生成 NPU 性能分数。
