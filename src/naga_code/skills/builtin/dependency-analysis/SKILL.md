---
name: dependency-analysis
description: 'Analyze project dependencies: inventory, outdated or vulnerable packages, unused or duplicated dependencies,
  and license or pinning issues.'
use_cases:
- audit dependencies
- find unused packages
- plan upgrades
inputs:
- manifest files (auto-detected)
tools:
- detect_project
- read_file
- search_code
- run_command
permissions: read-only; network commands (registry lookups/audits) need approval
validation:
- each claim about a version is from a lockfile/manifest or a real command
- unused-dependency claims checked with search_code
output: Dependency table (name, declared, locked, latest if checked, issues) and prioritized recommendations.
---

# Dependency analysis
1. Read manifests and lockfiles (pyproject.toml/uv.lock/requirements*.txt, package.json/lockfiles, Cargo.toml/Cargo.lock, go.mod).
2. Inventory direct vs transitive dependencies and version pinning strategy.
3. Detect unused dependencies by searching imports (`search_code`) for each direct dependency's import name.
4. Available audit tools (only with user approval, they use the network): `uv pip list --outdated`, `pip-audit`, `npm audit`,
   `npm outdated`, `cargo audit`, `govulncheck ./...`. If web tools are enabled, check registries for latest versions.
5. Recommend: remove unused, pin unpinned production deps, upgrade vulnerable ones with notes on breaking changes.
Do not install or upgrade anything without explicit approval.
