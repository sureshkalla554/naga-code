---
name: git-diff-review
description: Review the current git diff (or a commit range) for regressions, unintended changes, leftover debug
  code, and missing tests before committing.
use_cases:
- pre-commit self review
- check what changed
- write a commit message
inputs:
- optional base ref
tools:
- git_status
- git_diff
- git_log
- read_file
- find_references
permissions: read-only
validation:
- every changed file was inspected
- unintended changes (debug prints, secrets, large files) are flagged
output: Summary of the change, risks/regressions per file, missing tests, and a proposed commit message.
composes:
- code-review
---

# Git diff review
1. `git_status`, then `git_diff` (unstaged) and `git_diff staged=true`. For ranges use `git_compare`.
2. Classify each file's change: feature, fix, refactor, formatting, generated, accidental.
3. Flag: debug prints/logging, commented-out code, TODOs added, secrets or tokens, large/binary files, unrelated changes,
   changed public APIs without call-site updates (`find_references`), behavior changes without tests.
4. Propose a commit message: imperative subject <= 72 chars, body explaining why.
5. Do not stage or commit unless the user asks.
