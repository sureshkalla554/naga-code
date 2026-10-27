---
name: performance-profiling
description: 'Find and fix performance bottlenecks using measurement: profile, identify hot paths, optimize, and
  verify with before/after numbers.'
use_cases:
- slow endpoint or command
- high CPU/memory usage
- optimize a hot loop
inputs:
- the slow operation and how to reproduce it
tools:
- run_command
- read_file
- search_code
- edit_file
- run_tests
permissions: commands to run profilers/benchmarks (approval); workspace edits
validation:
- baseline and after measurements come from real runs
- behavior unchanged (tests pass)
output: Baseline numbers, profile hot spots, change made, after numbers, caveats about measurement noise.
---

# Performance profiling
1. Define the metric (latency, throughput, peak memory) and a reproducible benchmark command. Measure the baseline several times.
2. Profile with the language's tools (python: `python -m cProfile -s cumtime`, `py-spy` if installed; node: `--prof`; go: pprof; rust: `cargo flamegraph` if present).
3. Read the hot code. Look for algorithmic issues (N^2 loops, repeated I/O, N+1 queries, missing caching, unnecessary copies) before micro-optimizing.
4. Change one thing at a time; re-measure; keep only improvements that are larger than the noise.
5. Run tests to confirm behavior is unchanged. Report numbers honestly, including no-improvement results.
