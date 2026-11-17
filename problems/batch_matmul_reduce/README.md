# BatchMatmulMaxSum operator package

`task.md` contains only operator semantics, ABI and implementation requirements.
The framework owns sessions, feedback budgets, stopping and final reports.

- `harness/` is fixed Ascend C/CMake engineering. Only `kernel.asc` is submitted.
- `starters/` provides two tracks; `reference/` preserves the known best source.
- `cases.json` preserves 15 public performance shapes and 24 boundary cases.
- `judge.py` implements the package adapter. It generates fresh seeded input bytes,
  computes an FP64 reference from the stored FP16/BF16 values using the copied
  `trusted/cases.py`, and checks every output against judge-owned golden bytes.
- `protocol.json` freezes correctness and desired timing semantics.
- `provenance/` records source hashes and the old protocol without activating its
  timing measurements as new baselines.

The package can be discovered without NumPy, CANN or a device. Data generation
requires existing `numpy` and `ml_dtypes`; compilation/execution needs the original
Ascend C environment. CPU tests validate the adapter and orchestration; actual
CANN/NPU acceptance evidence is recorded separately by the server validation.

The 24 boundary shapes already existed in the inspectable old repository. Calling
their suite `hidden` means withholding its feedback and using resampled seeds; it
does not mean those shapes have never been published. Repeated same-shape calls
are captured individually; cross-shape same-process state transitions are not yet
covered.

The development NPU backend compiles fixed engineering, checks public cases for
check/bench and all public+boundary cases for final, and profiles all 15 public
shapes for bench/final. Each shape uses three independent captures, each with
8 warmups and 32 measured calls. Every call is poisoned, checked and independently
verified from its binary capture, including the profiled calls. Transfers and
checks are outside the measured device compute task, but affect the invocation
environment; reference and candidate must both use this exact protocol.

Scores require a newly calibrated baseline bound to package, engine, environment
and protocol identities. The old measurements must not be reused. The stored best
kernel is a baseline, not a theoretical bound; raw speedups remain available even
when the local 0–100 score is capped. Host timing never substitutes for msprof
device duration.

Formal scoring remains unavailable because hostile Host-code isolation and an
independently attested invocation association are not implemented. The development
score is explicitly not cheat-resistant. `security.operator_profile` remains
`legacy_host_untrusted`.
