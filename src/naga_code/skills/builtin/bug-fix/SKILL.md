---
name: bug-fix
description: Fix a diagnosed bug with a minimal change, add a regression test, and prove the fix with real test
  runs.
use_cases:
- fix a known bug
- make a failing test pass for the right reason
inputs:
- bug description or investigation result
tools:
- read_file
- edit_file
- apply_patch
- run_tests
- git_diff
permissions: workspace edits and test commands (approval per policy)
validation:
- a regression test fails before and passes after the fix (or this is explained)
- relevant existing tests pass
- diff is minimal
output: Root cause, the change (files + lines), regression test, before/after test results, residual risks.
composes:
- bug-investigation
---

# Bug fix
1. If the root cause is not established, load `bug-investigation` first.
2. Write or identify a regression test that captures the bug. Run it and confirm it FAILS for the expected reason.
3. Make the smallest correct change at the root cause (not at the symptom). Read the file first; use `edit_file`.
   Preserve existing style and public interfaces unless the bug requires changing them.
4. Re-run the regression test (must pass), then the surrounding test module/package.
5. Review your own diff with `git_diff`. Remove debugging code.
6. Never weaken assertions, skip tests or catch-and-ignore errors to get green results.
