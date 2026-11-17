# Minimal operator-package example

This is a portable, CPU-only integration fixture. It demonstrates that sessions,
budgets and reports need no vector-add knowledge. Its host wall-clock timings are
smoke-test diagnostics and must never become Ascend/NPU benchmark scores.

An administrator installs a trusted directory containing `problem.json`, task
description, fixed engineering, starters, test definitions/protocol and a trusted
judge adapter. The agent submits only the bytes of `kernel.asc`.

## Manifest

- `schema_version`, `id`, `version`: package schema and operator identity.
- `task`, `editable_file`, `tracks`: task text, exactly `kernel.asc`, and both
  `from_scratch` / `optimize` starter paths.
- `harness`: fixed engineering directory; it must not contain `kernel.asc`.
- `public_files`: optional destination-to-package-source mapping for public ABI
  headers. These files are copied into the fixed project and may be exported to
  the agent. Judge code, protocol, cases, reference and starter files cannot be
  exported through this field.
- `adapter`, `cases`, `protocol`: private judge code and frozen test/protocol
  definitions. A description without a runnable numerical oracle is insufficient.
- `build.commands`: argv lists, expanded with only `{project}` and `{build}`;
  the framework executes them without a shell. `build.runner` is relative to the
  build directory. Build tools and dependencies must already be installed.
- `candidate_policy`: size bound and an explicit list of literal include headers.
  This source check is advisory and cannot isolate arbitrary C++.
- `reference`: optional private baseline source, not a theoretical optimum.
- `security`: execution profile; this example is `cpu_example_untrusted`.

Every regular file, its relative name and its content participate in the package
digest, including hidden files. Symlinks, special files and traversal paths are
rejected. Do not generate caches or data inside an installed package. Updating a
test, starter, adapter or harness changes its identity and invalidates old pins.

## Trusted adapter contract

The module is administrator-owned executable code. The framework never imports
the candidate source. Standalone adapters may read helpers/data relative to their
own `__file__`, with no path assumptions about a checkout or current directory.

```python
cases(suite, seed) -> list[dict]
prepare(spec, outdir) -> dict
runner_args(spec, datadir, output, *, mode='correctness', repeat=1, warmup=0) -> list[str]
verify(spec, datadir, output, *, expected_calls=None) -> dict
```

`suite` is `public`, `hidden` or `all`. Every spec has a unique safe `case_id`,
the complete seeded data-generation parameters, and a `performance` boolean.
Only public performance cases are eligible for timing. Hidden inputs are
resampled from a controller-selected seed; the candidate does not select it.

`prepare` writes only into its judge-owned directory. Its manifest contains
`inputs` and `oracle_files` lists of relative filenames, plus artifact hashes.
The framework copies **only inputs** into the separate execution directory.
Golden files, the judge manifest and private reference never belong in that view.
The adapter is responsible for validating cache identity and numerical semantics.

`runner_args` returns arguments only, without the executable. It must not include
oracle paths. `mode` is correctness/profile, `repeat` is the measured-call count,
and `warmup` is additional calls. The framework fixes these values.

`verify` reads independently generated, judge-owned golden bytes and candidate
output bytes. It returns `passed: bool` and a diagnostic `status`. It does not use
candidate-provided JSON as correctness evidence. This fixture writes each
correctness invocation to `<output>.call-0`, `.call-1`, etc.; `expected_calls`
requires that many captures, including warmups. The final `<output>` is checked
as well. A real backend must additionally enforce its declared invocation and
measurement protocol. File counts are not proof against malicious Host code.

## Local smoke verification

From the `op_eval_v2` directory:

```text
python3 -m unittest discover -s tests -p test_problems.py -v
```

The tests compile and execute the fixed carrier using an existing `c++` compiler,
check all public/hidden FP32 outputs against an independent Python oracle, and
reject a wrong intermediate capture despite a correct final output. Missing a
compiler skips only the C++ integration tests. No model, server, device or package
installation is involved.
