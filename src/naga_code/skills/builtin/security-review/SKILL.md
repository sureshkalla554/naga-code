---
name: security-review
description: Audit code for security vulnerabilities (injection, authz, secrets, unsafe deserialization, path traversal,
  SSRF) with exploitability reasoning and remediations.
use_cases:
- security audit of a module or diff
- find unsafe SQL/shell usage
- review auth logic
inputs:
- 'scope: files, diff or whole repo'
tools:
- search_code
- read_file
- find_references
- git_diff
permissions: read-only
validation:
- each finding has a source-to-sink trace
- false positives are filtered by reading context
- no secrets are echoed in the report
output: Findings by severity with CWE category, location, data flow, exploit scenario, remediation; plus a list
  of areas not reviewed.
---

# Security review
1. Map entry points (HTTP handlers, CLI args, file/network input, message consumers) and sensitive sinks.
2. Search for risky patterns, then confirm each by reading the code:
   - SQL built with string formatting/concatenation (`execute(f"`, `+ "WHERE`), ORM raw queries
   - shell execution with user data (`shell=True`, `os.system`, `exec(`, backticks), `eval`/`pickle.loads`/`yaml.load` without SafeLoader
   - path joins with user input (traversal), open redirects, SSRF via user-controlled URLs
   - missing authorization checks, IDOR, weak session/JWT handling, CSRF
   - hard-coded credentials, secrets in logs, weak crypto (md5/sha1 for passwords, ECB, static IVs)
3. For each confirmed issue trace source -> sink, rate severity by exploitability and impact, and give a concrete fix
   (parameterized queries, argument arrays, allowlists, safe loaders).
4. Redact any secret values you find; report only their location.
