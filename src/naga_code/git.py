"""Git engine: read-only inspection helpers (argument arrays only, never shell strings)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitStatus:
    is_repo: bool
    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.untracked)

    def short(self) -> str:
        if not self.is_repo:
            return "not a git repository"
        parts = [self.branch or "(detached)"]
        if self.ahead or self.behind:
            parts.append(f"+{self.ahead}/-{self.behind}")
        counts = [f"{len(self.staged)} staged" if self.staged else "", f"{len(self.modified)} modified" if self.modified else "",
                  f"{len(self.untracked)} untracked" if self.untracked else ""]
        parts.append(", ".join(c for c in counts if c) or "clean")
        return " · ".join(parts)


def git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout,
                          env={"GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
                               "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())})


def status(root: Path) -> GitStatus:
    try:
        r = git(root, "status", "--porcelain=v1", "-b", "--untracked-files=normal")
    except (OSError, subprocess.TimeoutExpired):
        return GitStatus(False)
    if r.returncode != 0:
        return GitStatus(False)
    st = GitStatus(True, raw=r.stdout)
    for line in r.stdout.splitlines():
        if line.startswith("## "):
            head = line[3:]
            if head.startswith("No commits yet on "):
                st.branch = head[len("No commits yet on "):]
                continue
            branch, _, rest = head.partition("...")
            st.branch = None if branch.startswith("HEAD (no branch)") else branch.split(" ")[0]
            if rest:
                st.upstream = rest.split(" ")[0]
                if "ahead " in rest:
                    st.ahead = int(rest.split("ahead ")[1].split(",")[0].rstrip("]"))
                if "behind " in rest:
                    st.behind = int(rest.split("behind ")[1].rstrip("]"))
            continue
        x, y, path = line[0], line[1], line[3:]
        if x == "?":
            st.untracked.append(path)
            continue
        if x not in " ?":
            st.staged.append(path)
        if y not in " ?":
            st.modified.append(path)
    return st


def branch(root: Path) -> str | None:
    s = status(root)
    return s.branch if s.is_repo else None
