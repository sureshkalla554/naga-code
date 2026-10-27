---
name: build-failure-diagnosis
description: Diagnose a failing build, compile, CI or test run from its real output and fix the root cause.
use_cases:
- CI is red
- compile error
- dependency resolution failure
- flaky test
inputs:
- the failing command or CI log
tools:
- run_command
- run_tests
- read_output
- search_code
- read_file
- edit_file
permissions: commands and workspace edits (approval per policy)
validation:
- the failing command is re-run after the fix and its real result reported
- root cause stated, not just the symptom
output: Failing command, first real error, root cause, fix, re-run result.
---

# Build failure diagnosis
1. Reproduce locally with the exact command (from CI config in .github/workflows or the user). Capture full output; large
   output is stored as an artifact — use `read_output` with a pattern like `error|Error|FAILED` to find the FIRST error.
2. Classify: syntax/type error, missing dependency/version conflict, environment (tool version, env var, OS), test failure, flaky/timing.
3. Trace the first error to its cause in code or config. Check recent changes (`git_log`, `git_diff`).
4. Fix the cause with a minimal change. Re-run the same command and report the real result.
5. If it is environmental (missing system package, CUDA/driver), explain the required step and ask before system changes.
