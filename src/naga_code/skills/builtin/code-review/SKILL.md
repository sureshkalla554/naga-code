---
name: code-review
description: Review code or a diff for correctness bugs, regressions, security and maintainability issues; produce
  prioritized, evidence-backed findings.
use_cases:
- review a pull request or branch
- review the working-tree diff
- audit a module before release
inputs:
- 'scope: a diff, branch, commit range or set of files (default: current git diff)'
tools:
- git_diff
- git_compare
- read_file
- search_code
- find_references
- run_tests
permissions: read-only; may run tests with approval
validation:
- every finding cites file:line and a concrete failure scenario
- no finding is reported without reading the surrounding code
output: Findings ordered by severity (critical/high/medium/low), each with location, problem, scenario, suggested
  fix; then a short overall verdict.
---

# Code review
1. Establish scope. Default to `git_diff` (unstaged) plus `git_diff staged=true`; for a branch use `git_compare`.
   State the scope you reviewed.
2. For each changed hunk, read enough surrounding code (`read_file` with a range) to understand intent. Use
   `find_references` on changed functions to find callers that may break.
3. Load the checklist with `read_skill_file(name="code-review", path="references/review-checklist.md")` and walk it.
4. Only report issues you can justify from the code. For each: severity, `path:line`, what is wrong, a concrete
   input/state that triggers it, and the smallest fix. Mark uncertain items as "needs verification".
5. If tests exist for the touched area, offer to run them (or run them if the user asked); report real results.
6. Do not modify files during a review unless the user asks for fixes.
