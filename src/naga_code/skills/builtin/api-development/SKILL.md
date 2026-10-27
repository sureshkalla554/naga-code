---
name: api-development
description: Design and implement HTTP/REST API endpoints with validation, error handling, consistent status codes,
  and tests.
use_cases:
- add CRUD endpoints
- design a REST resource
- add request validation
inputs:
- resource/operations and framework in use
tools:
- read_file
- search_code
- write_file
- edit_file
- run_tests
permissions: workspace edits; test commands
validation:
- inputs validated; errors return correct status codes
- tests cover success, validation error, not found, auth
- no secrets hard-coded
output: Endpoint table (method, path, request, responses), files changed, tests and results.
composes:
- integration-test-generation
---

# API development
1. Detect the framework and existing patterns (routing, schemas/models, dependency injection, error format, auth).
2. Specify endpoints first: method, path, request schema, response schema, status codes (200/201/204, 400/422, 401/403, 404, 409).
3. Implement with the project's validation library; never trust client input; parameterize all queries.
4. Keep handlers thin; put logic in services where the project does so. Handle pagination and idempotency where relevant.
5. Add tests using the framework's test client for each status code path. Run them.
6. Update API docs/OpenAPI if present.
