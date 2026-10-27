---
name: integration-test-generation
description: Write integration tests that exercise real component boundaries (HTTP, DB, CLI, filesystem) with controlled,
  reproducible setup and teardown.
use_cases:
- test an API endpoint end to end
- test a CLI
- test database access
inputs:
- components/flows to cover
tools:
- read_file
- find_tests
- write_file
- run_tests
- start_process
- poll_process
- stop_process
permissions: workspace edits; may start local test servers (approval)
validation:
- setup/teardown leave no residue
- tests use local resources only
- tests pass when run twice in a row
output: Test files added, the flows covered, how to run them, real run results.
composes:
- unit-test-generation
---

# Integration test generation
1. Identify the boundary under test and how the project already sets up integration tests (fixtures, docker-compose, test DB, test client).
2. Prefer in-process test clients (e.g. ASGI/WSGI test clients) over spawning servers. If a server is required use
   `start_process`, wait by polling for readiness, and always `stop_process` afterwards.
3. Use temporary directories/databases and unique names; never point at production or shared resources.
4. Cover: the main success flow, auth/validation failures, not-found, and one concurrency or idempotency case if relevant.
5. Run the tests twice to catch leaked state. Report real results.
