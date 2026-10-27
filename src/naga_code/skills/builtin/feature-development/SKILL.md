---
name: feature-development
description: 'Implement a new feature end to end: clarify requirements, plan, implement in small steps following
  project conventions, test and document.'
use_cases:
- add an endpoint, command, option or module
- implement an approved plan
inputs:
- feature description and acceptance criteria
tools:
- update_plan
- update_step
- read_file
- write_file
- edit_file
- run_tests
- run_lint
permissions: workspace edits; test/lint commands
validation:
- all acceptance criteria are checked off with evidence
- new code has tests
- lint/tests pass or failures are reported
output: Plan with step status, files changed, tests added, validation results, follow-ups.
composes:
- unit-test-generation
- documentation
---

# Feature development
1. Extract acceptance criteria from the request. If something essential is ambiguous, ask once; otherwise state your assumption.
2. Explore conventions: similar existing features, project layout, test style, naming. Reuse existing helpers.
3. Record a plan with `update_plan` (objective, acceptance criteria, steps, files, risks, validation commands).
4. Implement step by step; mark each step `running`/`completed` with `update_step` as work actually happens.
5. Add tests next to existing ones (unit first, integration where the project has them). Run them.
6. Run the project's linter if configured. Update docs/README/CHANGELOG if the project keeps them.
7. Final answer: map each acceptance criterion to evidence (file, test name, command result).
