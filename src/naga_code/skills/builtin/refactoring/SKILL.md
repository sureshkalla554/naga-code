---
name: refactoring
description: 'Restructure code without changing behavior: establish a safety net of tests, make small verified steps,
  keep public interfaces stable.'
use_cases:
- extract function/module
- rename across files
- remove duplication
- simplify complex code
inputs:
- target code and the refactoring goal
tools:
- find_references
- read_file
- apply_patch
- edit_file
- run_tests
- git_diff
permissions: workspace edits; test commands
validation:
- tests pass before and after each step
- no behavior change unless agreed
- all references updated (find_references returns none stale)
output: Refactoring steps performed, files changed, test results after each step.
composes:
- unit-test-generation
---

# Refactoring
1. Run the relevant tests first to get a green baseline. If none exist, add characterization tests (see unit-test-generation) or tell the user the risk.
2. Find every reference to the code you will move or rename (`find_references`, `search_code` for string uses, configs, docs).
3. Change in small steps; after each, run the tests. Use `apply_patch` for coordinated multi-file edits.
4. Do not mix refactoring with behavior changes or formatting churn.
5. Finish with `git_diff` review and confirm no stale references remain.
