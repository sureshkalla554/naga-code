#!/usr/bin/env bash
# Small reproducible benchmark: runs fixed tasks headlessly against the configured local model
# in throwaway copies of tiny projects, and reports success / validation / wall time.
#   scripts/benchmark.sh [--model NAME] [--runs N]
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL="" RUNS=1 KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in --model) MODEL="$2"; shift 2;; --runs) RUNS="$2"; shift 2;; --keep) KEEP=1; shift;; *) echo "unknown arg $1"; exit 2;; esac
done
NAGA=(uv run naga)
PY="$(pwd)/.venv/bin/python"  # same interpreter the agent sees on PATH under `uv run`
[[ -n "$MODEL" ]] && NAGA+=(--model "$MODEL")
WORK="$(mktemp -d)"
if [[ $KEEP == 1 ]]; then echo "work dir: $WORK"; else trap 'rm -rf "$WORK"' EXIT; fi
export NAGA_HOME="$WORK/home"

make_project() {  # $1 = dir, $2 = task id
  mkdir -p "$1"; cd "$1"; git init -q
  case "$2" in
    bugfix)
      printf 'def add(a, b):\n    return a - b\n' > calc.py
      printf 'from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_calc.py ;;
    feature)
      printf 'def slugify(s):\n    raise NotImplementedError\n' > text.py
      printf 'from text import slugify\n\ndef test_slug():\n    assert slugify("Hello World!") == "hello-world"\n\ndef test_spaces():\n    assert slugify("  a  b ") == "a-b"\n' > test_text.py ;;
    multifile)
      mkdir -p app; printf 'from app.util import double\n\ndef total(xs):\n    return sum(double(x) for x in xs)\n' > app/core.py
      printf 'def double(x):\n    return x * 3\n' > app/util.py; : > app/__init__.py
      printf 'from app.core import total\n\ndef test_total():\n    assert total([1, 2]) == 6\n' > test_core.py ;;
  esac
  git add -A && git -c user.email=b@x -c user.name=b commit -qm init; cd - >/dev/null
}

declare -A PROMPT=(
  [bugfix]="test_calc.py fails. Fix the bug and run the tests."
  [feature]="Implement slugify in text.py so the tests in test_text.py pass, then run them."
  [multifile]="test_core.py fails. Find the root cause (it may be in another module), fix it, and run the tests."
)
printf "%-10s %-4s %-10s %-10s %8s\n" task run stop tests seconds
for task in bugfix feature multifile; do
  for run in $(seq 1 "$RUNS"); do
    dir="$WORK/$task-$run"; make_project "$dir" "$task"
    start=$(date +%s.%N)
    out=$("${NAGA[@]}" --cwd "$dir" -p "${PROMPT[$task]}" --json --allow-edits \
          --allow-commands "python3 -m pytest" --allow-commands "pytest" 2>"$dir.log" || true)
    echo "$out" > "$dir.json"
    secs=$(echo "$(date +%s.%N) - $start" | bc)
    stop=$(echo "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stop_reason","?"))' 2>/dev/null || echo "crash")
    # independent check: run the tests ourselves, never trust the agent's claim
    if (cd "$dir" && "$PY" -m pytest -q -p no:cacheprovider >/dev/null 2>&1); then tests=pass; else tests=FAIL; fi
    printf "%-10s %-4s %-10s %-10s %8.1f\n" "$task" "$run" "$stop" "$tests" "$secs"
  done
done
