"""Background sessions: a saved session continued by a detached headless `naga -p` process.

In a git project each job works in its own git worktree on a branch ``naga/<job>``, starting from your current
files (uncommitted changes included). Because it is isolated, the job may edit files without asking; nothing
reaches your working tree until you review ``job_diff`` and run ``apply_job``. Commands that need approval are
still denied (headless). Jobs are recorded in ``<data>/background/jobs.json``; output goes to a log file.
"""

from __future__ import annotations

import json
import os
import shutil
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
    worktree: str = ""     # isolated checkout the job works in ("" = ran in the project itself)
    branch: str = ""
    snapshot: str = ""     # commit in the job branch holding your uncommitted changes at start

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


# Caches and build output a job's tests may create; never part of its changes, even without a .gitignore.
JUNK = [f":(exclude,glob)**/{p}" for p in ("__pycache__/**", "*.pyc", ".pytest_cache/**", ".mypy_cache/**",
                                          ".ruff_cache/**", "node_modules/**", ".venv/**", "*.egg-info/**")]


def _git(cwd: Path, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=Naga", "-c", "user.email=naga@localhost", *args], cwd=cwd,
                          input=input, capture_output=True, timeout=120)


def create_worktree(project: Path, jid: str) -> tuple[Path, str, str] | None:
    """Isolated checkout of `project` (HEAD plus your uncommitted and untracked files); None if not possible."""
    if _git(project, "rev-parse", "--verify", "HEAD").returncode != 0:
        return None   # not a git repository, or no commits yet
    wt, branch = data_home() / "worktrees" / jid, f"naga/{jid}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _git(project, "worktree", "add", "-b", branch, str(wt), "HEAD").returncode != 0:
        return None
    patch = _git(project, "diff", "HEAD", "--binary").stdout
    if patch.strip():
        _git(wt, "apply", "--whitespace=nowarn", input=patch)
    untracked = _git(project, "ls-files", "--others", "--exclude-standard", "-z").stdout.decode().split("\0")
    for rel in filter(None, untracked):
        src = project / rel
        if src.is_file() and src.stat().st_size < 50_000_000:
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, wt / rel)
    _git(wt, "add", "-A")
    _git(wt, "commit", "--no-verify", "--allow-empty", "-q", "-m", f"naga {jid}: snapshot of your working tree")
    snapshot = _git(wt, "rev-parse", "HEAD").stdout.decode().strip()
    return wt, branch, snapshot


def job_diff(job: Job, stat: bool = False) -> str:
    """What the job changed in its worktree since it started (empty if nothing / not a worktree job)."""
    if not job.worktree or not Path(job.worktree).is_dir():
        return ""
    wt = Path(job.worktree)
    _git(wt, "add", "-A", "-N", "--", ".", *JUNK)   # show new files too (only touches the job's own index)
    r = _git(wt, "diff", job.snapshot, *(["--stat"] if stat else ["--binary"]), "--", ".", *JUNK)
    return r.stdout.decode(errors="replace")


def apply_job(job: Job, project: Path) -> tuple[bool, str]:
    patch = job_diff(job)
    if not patch.strip():
        return False, "the job made no changes"
    # The patch is relative to a snapshot of your working tree at job start, so it normally applies as-is; if you
    # changed the same files since, fall back to a 3-way merge (needs those files' index to match).
    r = _git(project, "apply", "--whitespace=nowarn", input=patch.encode())
    if r.returncode != 0:
        r = _git(project, "apply", "--3way", "--whitespace=nowarn", input=patch.encode())
    if r.returncode != 0:
        return False, ("could not apply cleanly (you changed the same lines since the job started):\n"
                       + (r.stderr or r.stdout).decode(errors="replace")[-1500:])
    return True, "applied the job's changes to your working tree (review with git diff; nothing was committed)"


def discard_job(job: Job, project: Path) -> str:
    if job.worktree:
        _git(project, "worktree", "remove", "--force", job.worktree)
        shutil.rmtree(job.worktree, ignore_errors=True)
        _git(project, "worktree", "prune")
    if job.branch.startswith("naga/"):
        _git(project, "branch", "-D", job.branch)
    _save([j for j in jobs() if j.id != job.id])
    return f"removed {job.id}" + (f" (worktree and branch {job.branch})" if job.worktree else "")


def start(project: Path, session_id: str, prompt: str, *, label: str = "", model: str | None = None,
          extra_args: list[str] | None = None, isolate: bool = True, phone: bool = False) -> Job:
    jid = time.strftime("bg-%H%M%S-") + os.urandom(2).hex()
    log = _dir() / f"{jid}.log"
    wt_info = create_worktree(project, jid) if isolate else None
    workdir = project
    flags = list(extra_args or [])
    if wt_info:
        workdir = wt_info[0]
        from .memory.sessions import SessionStore
        src, dst = SessionStore(project).path(session_id), SessionStore(workdir).path(session_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, dst)
        flags.append("--allow-edits")   # isolated worktree: edits are safe until you apply them
    if phone:
        flags.append("--phone-approvals")
    argv = [sys.executable, "-c", "import sys; from naga_code.cli import main; sys.exit(main())",
            "--cwd", str(workdir), "--resume", session_id, "-p", prompt] + (["--model", model] if model else []) \
        + flags
    with open(log, "w") as out:
        out.write(f"# naga background job {jid}\n# session {session_id}\n# prompt: {prompt}\n\n")
        out.flush()
        proc = subprocess.Popen(argv, cwd=workdir, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, "NAGA_JOB_ID": jid})
    job = Job(jid, session_id, str(project), prompt, proc.pid, str(log), time.time(), label,
              *((str(wt_info[0]), wt_info[1], wt_info[2]) if wt_info else ()))
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
