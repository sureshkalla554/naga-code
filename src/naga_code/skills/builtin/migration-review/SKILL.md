---
name: migration-review
description: 'Review database schema/data migrations for safety: locking, data loss, reversibility, ordering and
  deploy compatibility.'
use_cases:
- review a new migration
- plan a risky schema change
inputs:
- migration files or the intended schema change
tools:
- read_file
- search_code
- git_diff
- find_files
permissions: read-only
validation:
- each risk is tied to a specific statement
- rollback path is stated
- compatibility with running app version is analyzed
output: Per-migration risk table (statement, risk, severity, mitigation) and a go/no-go recommendation.
---

# Database migration review
1. Find migrations (`find_files` for migrations/, alembic/versions, db/migrate, prisma/migrations) and the ORM models they correspond to.
2. For each statement check: table rewrites/long locks (adding NOT NULL without default, type changes, index creation without CONCURRENTLY),
   data loss (DROP COLUMN/TABLE, narrowing types, truncation), irreversible down-migrations, ordering/dependency issues, large backfills in one transaction.
3. Check deploy compatibility: can the old app version run against the new schema (expand/contract pattern)?
4. Check the code that reads/writes the changed columns (`search_code`).
5. Recommend safer sequences (add nullable -> backfill in batches -> add constraint; create index concurrently).
Never run migrations against real databases during review.
