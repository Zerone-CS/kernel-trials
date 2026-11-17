// Fixed CPU integration carrier. It has no golden data. Host C++ shares this
// process, so its timers and checks are NOT a malicious-code security boundary.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include "operator_contract.h"
#include "kernel.asc"

namespace {
constexpr std::size_t kGuardCount = 16;
constexpr float kGuard = 123456.75f;

std::size_t count(const std::string& text, std::size_t maximum, bool zero) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("invalid integer argument");
    unsigned long long value = std::stoull(text);
    if (value > maximum || (!zero && value == 0))
        throw std::runtime_error("integer argument out of bounds");
    return static_cast<std::size_t>(value);
}

bool guards_ok(const std::vector<float>& values, std::size_t n) {
    for (std::size_t i = 0; i < kGuardCount; ++i)
        if (values[i] != kGuard || values[kGuardCount + n + i] != kGuard) return false;
    return true;
}

void write_output(const std::string& path, const float* values, std::size_t n) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("cannot open output");
    stream.write(reinterpret_cast<const char*>(values), n * sizeof(float));
    if (!stream) throw std::runtime_error("cannot write output");
}
}  // namespace

int main(int argc, char** argv) {
    try {
        std::map<std::string, std::string> args;
        for (int i = 1; i < argc; i += 2) {
            if (i + 1 >= argc) throw std::runtime_error("missing argument value");
            const std::string key = argv[i];
            if (key != "--input" && key != "--output" && key != "--n" &&
                key != "--mode" && key != "--repeat" && key != "--warmup")
                throw std::runtime_error("unknown argument");
            if (!args.emplace(key, argv[i + 1]).second)
                throw std::runtime_error("duplicate argument");
        }
        for (const auto* key : {"--input", "--output", "--n"})
            if (args.find(key) == args.end()) throw std::runtime_error("required argument missing");
        const std::size_t n = count(args.at("--n"), 1048576, false);
        const std::size_t repeat = count(args.count("--repeat") ? args.at("--repeat") : "1", 100000, false);
        const std::size_t warmup = count(args.count("--warmup") ? args.at("--warmup") : "0", 10000, true);
        const std::string mode = args.count("--mode") ? args.at("--mode") : "correctness";
        if (mode != "correctness" && mode != "profile") throw std::runtime_error("invalid mode");

        std::ifstream input(args.at("--input"), std::ios::binary | std::ios::ate);
        if (!input || input.tellg() != static_cast<std::streamoff>(2 * n * sizeof(float)))
            throw std::runtime_error("input byte count mismatch");
        input.seekg(0);
        std::vector<float> original(2 * n);
        input.read(reinterpret_cast<char*>(original.data()), original.size() * sizeof(float));
        if (!input) throw std::runtime_error("cannot read input");
        std::vector<float> x(n + 2 * kGuardCount, kGuard), z(x), y(x);
        std::copy(original.begin(), original.begin() + n, x.begin() + kGuardCount);
        std::copy(original.begin() + n, original.end(), z.begin() + kGuardCount);
        float* output = y.data() + kGuardCount;
        std::cout << std::setprecision(17);
        for (std::size_t i = 0; i < warmup + repeat; ++i) {
            // Every call receives poisoned output, including timed calls. This
            // catches ordinary incomplete writes, not adversarial host code.
            std::fill(output, output + n, std::numeric_limits<float>::quiet_NaN());
            const auto start = std::chrono::steady_clock::now();
            run_kernel(x.data() + kGuardCount, z.data() + kGuardCount, output, n);
            const auto stop = std::chrono::steady_clock::now();
            const double elapsed = std::chrono::duration<double, std::micro>(stop - start).count();
            if (!guards_ok(x, n) || !guards_ok(z, n) || !guards_ok(y, n))
                throw std::runtime_error("guard corruption");
            if (std::memcmp(x.data() + kGuardCount, original.data(), n * sizeof(float)) ||
                std::memcmp(z.data() + kGuardCount, original.data() + n, n * sizeof(float)))
                throw std::runtime_error("input mutated");
            if (std::any_of(output, output + n, [](float value) { return !std::isfinite(value); }))
                throw std::runtime_error("output not fully written or nonfinite");
            const bool is_warmup = i < warmup;
            if (mode == "correctness")
                write_output(args.at("--output") + ".call-" + std::to_string(i), output, n);
            std::cout << "{\"type\":\"call\",\"iteration\":" << i
                      << ",\"warmup\":" << (is_warmup ? "true" : "false")
                      << ",\"elapsed_us\":" << elapsed
                      << ",\"metric\":\"cpu_host_wall_us\"}\n";
        }
        write_output(args.at("--output"), output, n);
        std::cout << "{\"type\":\"summary\",\"status\":\"ok\",\"mode\":\"" << mode
                  << "\",\"measured_calls\":" << repeat << ",\"warmup_calls\":" << warmup
                  << ",\"input_unchanged\":true,\"guards_ok\":true,\"output_finite\":true,"
                     "\"metric\":\"cpu_host_wall_us\",\"testing_only\":true}\n";
        return 0;
    } catch (const std::exception&) {
        // Do not copy unescaped candidate exception text into the JSON stream.
        std::cerr << "{\"type\":\"error\",\"status\":\"failed\",\"message\":\"runner rejected execution\"}\n";
        return 1;
    }
}
