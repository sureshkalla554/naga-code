#!/usr/bin/env python3
"""Quality regression suite: run Naga on the 10 benchmark tasks, score them with hidden checkers, compare.

    uv run python bench/regression.py                      # run, print a table, compare with bench/baseline.json
    uv run python bench/regression.py --save-baseline      # make this run the new baseline
    uv run python bench/regression.py --model qwen3-coder:latest t02 t07

Each task gets a fresh folder, the same prompt every time, and permission to edit files and run
python3/pytest/ls/cat. A task whose score drops below its baseline is reported, and the exit status is 1.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tasks import TASKS, materialize  # noqa: E402


def env_with_pytest(base: Path) -> dict:
    """A python3 with pytest first on PATH, for the agent's own test runs (the checkers use this interpreter)."""
    v = base / "venv"
    if not (v / "bin" / "pytest").exists():
        venv.create(v, with_pip=True)
        subprocess.run([str(v / "bin" / "pip"), "install", "-q", "pytest"], check=True)
    return {**os.environ, "PATH": f"{v / 'bin'}:{os.environ['PATH']}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", default=list(TASKS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--naga", default="naga", help="command to run Naga")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--out", default=str(HERE / "last-run.json"))
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="naga-bench-"))
    env = env_with_pytest(Path(tempfile.gettempdir()) / "naga-bench-env")
    results = {}
    for tid in args.tasks:
        t = TASKS[tid]
        d = work / tid
        materialize(tid, d)
        cmd = [*args.naga.split(), "--cwd", str(d), "-p", t["prompt"], "--json", "--allow-edits"]
        if args.model:
            cmd += ["--model", args.model]
        for c in ("python3", "pytest", "ls", "cat"):
            cmd += ["--allow-commands", c]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=d, capture_output=True, text=True, env=env, timeout=args.timeout)
            stop = json.loads(p.stdout).get("stop_reason", "?") if p.stdout.strip().startswith("{") else f"exit {p.returncode}"
        except subprocess.TimeoutExpired:
            stop = "timeout"
        secs = round(time.monotonic() - t0, 1)
        score, notes = t["check"](d)
        results[tid] = {"title": t["title"], "score": score, "seconds": secs, "stop": stop, "notes": notes}
        print(f"{tid} {t['title']:40} {score:3}  {secs:6.1f}s  {notes}", flush=True)
    Path(args.out).write_text(json.dumps(results, indent=1))
    baseline_file = HERE / "baseline.json"
    worse = []
    if baseline_file.exists() and not args.save_baseline:
        base = json.loads(baseline_file.read_text())
        print("\ncompared with baseline:")
        for tid, r in results.items():
            b = base.get(tid)
            if b:
                delta = r["score"] - b["score"]
                mark = "WORSE" if delta < 0 else ("better" if delta > 0 else "same")
                print(f"  {tid} score {b['score']:3} -> {r['score']:3} ({mark})   time {b['seconds']:6.1f}s -> {r['seconds']:6.1f}s")
                if delta < 0:
                    worse.append(tid)
    total = sum(r["score"] for r in results.values())
    print(f"\ntotal score {total}/{100 * len(results)} · total time {sum(r['seconds'] for r in results.values()):.0f}s")
    if args.save_baseline:
        baseline_file.write_text(json.dumps(results, indent=1))
        print(f"baseline saved to {baseline_file}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
