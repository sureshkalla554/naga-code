---
name: bug-investigation
description: 'Investigate a reported bug: reproduce it, trace the root cause through the code, and report evidence
  without changing code yet.'
use_cases:
- unexpected behavior report
- failing test triage
- error message or stack trace analysis
inputs:
- symptom description, error text, or failing test
tools:
- search_code
- find_symbol
- find_references
- read_file
- run_tests
- run_command
permissions: read-only plus approved commands to reproduce
validation:
- root cause is tied to specific code lines
- reproduction steps were actually executed or explicitly marked as not executed
output: Symptom, reproduction (command + real output), root cause with file:line, contributing factors, proposed
  fix options.
composes:
- bug-fix
---

# Bug investigation
1. Restate the symptom precisely. Extract exact error strings and search for them with `search_code`.
2. Reproduce: find the smallest failing test or command (`find_tests`, `detect_project`) and run it. Record the real output.
   If you cannot reproduce, say so and continue with static analysis.
3. Trace: from the failure point follow the data backwards (`find_symbol`, `find_references`, `read_file`).
   Form a hypothesis, then confirm it with evidence (a read, a narrower test, a log line). Discard disproved hypotheses explicitly.
4. Check recent history for the area (`git_log paths=[...]`) when a regression is suspected.
5. Report the root cause with file:line, why it happens, and 1-3 fix options with trade-offs. Do not edit yet;
   hand off to the `bug-fix` skill when the user wants the fix.
