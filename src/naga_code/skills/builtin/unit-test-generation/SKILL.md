---
name: unit-test-generation
description: Write focused unit tests for existing code using the project's test framework and conventions, covering
  normal, edge and error cases.
use_cases:
- increase coverage for a module
- add tests before refactoring
inputs:
- target file, class or function
tools:
- read_file
- find_tests
- detect_project
- write_file
- edit_file
- run_tests
permissions: workspace edits; test commands
validation:
- tests run and pass against current code
- each test asserts behavior, not implementation details
- no network/GPU/time dependence without fakes
output: List of test cases added with what each covers, and the real test run result.
---

# Unit test generation
1. `detect_project` and `find_tests` to learn the framework, file naming, fixtures and helpers already in use.
2. Read the target code. List behaviors: happy path, boundaries (empty, zero, max, unicode), invalid input, error paths.
3. Write tests in the existing style and location. Use the project's fixtures/mocks; fake external services, clocks and randomness.
4. Run only the new tests first, then the module's suite. If a test fails, decide whether the test or the code is wrong;
   report suspected bugs instead of encoding wrong behavior into tests.
5. Keep tests deterministic and independent.
