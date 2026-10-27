"""Background sessions: a saved session continued by a detached headless `naga -p` process.

Background runs have no interactive approver, so anything that needs approval is denied and reported in the
log (never silently allowed). Jobs are recorded in ``<data>/background/jobs.json``; output goes to a log file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import data_home


@dataclass
class Job:
    id: str
    session: str
    project: str
    prompt: str
    pid: int
    log: str
    started: float
    label: str = ""

    def running(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        try:  # a finished child that nobody reaped yet is a zombie, not a running job
            with open(f"/proc/{self.pid}/stat") as f:
                return f.read().split(")")[-1].split()[0] != "Z"
        except OSError:
            return True

    def tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(Path(self.log).read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


def _dir() -> Path:
    d = data_home() / "background"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs() -> list[Job]:
    p = _dir() / "jobs.json"
    try:
        return [Job(**j) for j in json.loads(p.read_text())]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _save(all_jobs: list[Job]) -> None:
    p = _dir() / "jobs.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(j) for j in all_jobs[-100:]], indent=1))
    os.replace(tmp, p)


def start(project: Path, session_id: str, prompt: str, *, label: str = "", model: str | None = None,
          extra_args: list[str] | None = None) -> Job:
    jid = time.strftime("bg-%H%M%S-") + os.urandom(2).hex()
    log = _dir() / f"{jid}.log"
    argv = [sys.executable, "-c", "import sys; from naga_code.cli import main; sys.exit(main())",
            "--cwd", str(project), "--resume", session_id, "-p", prompt] + (["--model", model] if model else []) \
        + list(extra_args or [])
    with open(log, "w") as out:
        out.write(f"# naga background job {jid}\n# session {session_id}\n# prompt: {prompt}\n\n")
        out.flush()
        proc = subprocess.Popen(argv, cwd=project, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    job = Job(jid, session_id, str(project), prompt, proc.pid, str(log), time.time(), label)
    _save(jobs() + [job])
    return job


def find(prefix: str) -> Job | None:
    matches = [j for j in jobs() if j.id.startswith(prefix) or j.session.startswith(prefix)]
    return matches[-1] if matches else None


def stop(job: Job) -> bool:
    if not job.running():
        return False
    try:
        os.killpg(job.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True
