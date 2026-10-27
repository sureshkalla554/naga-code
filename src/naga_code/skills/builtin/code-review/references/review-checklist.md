# Review checklist

## Correctness
- [ ] Off-by-one, boundary and empty-input handling
- [ ] Null/None/undefined handling; optional values unwrapped safely
- [ ] Error paths: exceptions caught at the right level, not swallowed; resources closed (files, sockets, locks)
- [ ] Concurrency: shared state, races, missing awaits, lock ordering, async cancellation
- [ ] Changed function signatures/behavior: all call sites updated
- [ ] Time zones, encodings, locale, floating point comparisons

## Security
- [ ] Untrusted input validated; queries parameterized; no shell string building with input
- [ ] Authorization checked on every new code path
- [ ] No secrets, tokens or credentials committed or logged

## Tests
- [ ] New behavior covered; bug fixes have a regression test
- [ ] Tests are deterministic (no sleeps, real network, wall-clock dependence)

## Maintainability
- [ ] Follows existing conventions and naming
- [ ] No dead code, debug output or unrelated changes
- [ ] Public API/docs/changelog updated where needed

## Performance
- [ ] No N+1 queries, unbounded loops over large data, or repeated expensive work in hot paths
