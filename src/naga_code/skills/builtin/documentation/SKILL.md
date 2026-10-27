---
name: documentation
description: Write or update documentation (README, module docs, docstrings, usage guides) that is accurate to the
  current code and verified examples.
use_cases:
- document a module or API
- update README after a change
- write a how-to
inputs:
- what to document and for whom
tools:
- read_file
- search_code
- edit_file
- write_file
- run_command
permissions: workspace edits; may run example commands with approval
validation:
- every command/example in the docs was run or is marked untested
- docs match current signatures and flags
output: Docs changed/added and which examples were verified.
---

# Documentation
1. Identify the audience (user, contributor, operator) and the existing doc style/location.
2. Derive facts from code, not memory: read signatures, CLI argument definitions, config defaults.
3. Write concise sections: purpose, install/setup, usage with examples, configuration, troubleshooting.
4. Verify examples by running them where safe; otherwise mark them clearly as untested.
5. Keep docstrings consistent with the project's convention (Google/NumPy/reST/JSDoc).
