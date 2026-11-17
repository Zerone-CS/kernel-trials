#pragma once
#include <cstddef>

// Fixed ABI supplied by the engineering harness. Implement only kernel.asc.
void run_kernel(const float* x, const float* z, float* y, std::size_t n);
