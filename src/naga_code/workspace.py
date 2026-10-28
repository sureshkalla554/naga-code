"""Filesystem engine: project boundaries, ignore rules, safe text I/O and change tracking."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pathspec


class PathError(Exception):
    pass


class StaleFileError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_project_root(start: Path) -> Path:
    """Nearest ancestor containing .git or NAGA.md, else ``start`` itself.

    The search stops below the home directory: a stray ~/.git must not turn all of $HOME into the project."""
    start = start.resolve()
    home = Path.home().resolve()
    for p in [start, *start.parents]:
        if p == home and p != start:
            break
        if (p / ".git").exists() or (p / "NAGA.md").is_file():
            return p
    return start


def is_binary_bytes(data: bytes) -> bool:
    if not data:
        return False
    chunk = data[:8192]
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        if e.start > len(chunk) - 4:  # truncated multibyte char at chunk boundary
            return False
        text_chars = sum(1 for b in chunk if 32 <= b < 127 or b in b"\n\r\t\b\f")
        return text_chars / len(chunk) < 0.85


@dataclass
class TextFile:
    text: str            # always "\n" line endings
    encoding: str        # utf-8 | utf-8-sig | latin-1
    newline: str         # "\n" or "\r\n"
    digest: str          # sha256 of the raw bytes on disk

    def encode(self, text: str) -> bytes:
        if self.newline == "\r\n":
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        return text.encode(self.encoding)


def decode_text(raw: bytes) -> TextFile:
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        enc = "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        enc, text = "latin-1", raw.decode("latin-1")
    newline = "\r\n" if "\r\n" in text and text.count("\r\n") >= text.count("\n") / 2 else "\n"
    return TextFile(text.replace("\r\n", "\n"), enc, newline, sha256(raw))


@dataclass
class ChangeRecord:
    id: int
    path: str                   # workspace-relative
    before: bytes | None        # None = file did not exist
    after: bytes | None         # None = file deleted
    tool: str
    task_id: str | None = None
    agent: str = "main"

    def summary(self) -> str:
        if self.before is None:
            return f"created {self.path}"
        if self.after is None:
            return f"deleted {self.path}"
        return f"modified {self.path}"


class Workspace:
    def __init__(self, root: Path, exclude: list[str] | None = None, extra_read_dirs: list[str] | None = None):
        self.root = root.resolve()
        self.extra_read_dirs = [Path(os.path.expanduser(d)).resolve() for d in (extra_read_dirs or [])]
        self.extra_write_dirs: list[Path] = []   # added by the user for this session (/add-dir)
        self.exclude_patterns = list(exclude or [])
        self._exclude = pathspec.PathSpec.from_lines("gitignore", self.exclude_patterns)
        self._gitignore = self._load_gitignore()
        self.read_digests: dict[Path, str] = {}   # last content the agent saw / wrote
        self.changes: list[ChangeRecord] = []
        self._next_change = 1
        self.write_lock = asyncio.Lock()
        self.current_task: str | None = None

    # ---- boundaries -----------------------------------------------------
    def _load_gitignore(self) -> pathspec.PathSpec:
        lines: list[str] = []
        gi = self.root / ".gitignore"
        if gi.is_file():
            lines += gi.read_text(errors="replace").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)

    def reload_ignores(self) -> None:
        self._gitignore = self._load_gitignore()

    def resolve(self, path: str | os.PathLike, *, write: bool = False) -> Path:
        """Resolve a user/model-supplied path and enforce project boundaries.

        Symlinks are resolved before the check, so a link that points outside
        the workspace is rejected just like ``../`` traversal.
        """
        raw = os.path.expanduser(str(path)).strip()
        if not raw:
            raise PathError("empty path")
        if "\x00" in raw:
            raise PathError("path contains NUL byte")
        p = Path(raw)
        if not p.is_absolute():
            p = self.root / p
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError) as e:
            raise PathError(f"cannot resolve {path}: {e}") from e
        if resolved == self.root or self.root in resolved.parents:
            if write and (resolved.relative_to(self.root).parts[:1] == (".git",)):
                raise PathError("refusing to modify the .git directory directly")
            return resolved
        allowed = self.extra_write_dirs + ([] if write else self.extra_read_dirs)
        if any(resolved == d or d in resolved.parents for d in allowed):
            return resolved
        raise PathError(f"path '{path}' is outside the project ({self.root})")

    def add_dir(self, path: str, writable: bool = True) -> Path:
        d = Path(os.path.expanduser(path)).resolve()
        if not d.is_dir():
            raise PathError(f"not a directory: {path}")
        if d == Path("/") or d == Path.home().resolve():
            raise PathError(f"refusing to add {d}: too broad")
        (self.extra_write_dirs if writable else self.extra_read_dirs).append(d)
        return d

    def _rel_str(self, p: str) -> str | None:
        """Path string relative to the root, or None when outside it (string ops: pathlib's relative_to
        dominated walking large trees)."""
        root = str(self.root)
        if p == root:
            return ""
        prefix = root if root.endswith(os.sep) else root + os.sep
        return p[len(prefix):] if p.startswith(prefix) else None

    def rel(self, p: Path) -> str:
        r = self._rel_str(str(p))
        return str(p) if r is None else (r or ".")

    def is_excluded(self, p: Path) -> bool:
        rel = self._rel_str(str(p))
        return False if rel is None else self._excluded_rel(rel, p.is_dir())

    def _excluded_rel(self, rel: str, is_dir: bool) -> bool:
        s = rel + ("/" if is_dir else "")
        return self._exclude.match_file(s) or any(self._exclude.match_file(part + "/")
                                                  for part in rel.split(os.sep)[:-1]) \
            or self._gitignore.match_file(s)

    def walk(self, start: Path | None = None, include_ignored: bool = False, max_files: int = 50_000,
             deadline: float | None = None) -> Iterator[Path]:
        """Files under ``start``; stops after ``max_files`` or once ``time.monotonic()`` passes ``deadline``."""
        start = start or self.root
        count = 0
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            if deadline is not None and time.monotonic() > deadline:
                return
            d = Path(dirpath)
            drel = self._rel_str(dirpath)
            join = (lambda n: n) if not drel else (lambda n: drel + os.sep + n)
            if include_ignored:
                dirnames[:] = sorted(n for n in dirnames if n != ".git")
            elif drel is None:
                dirnames[:] = sorted(n for n in dirnames if not self.is_excluded(d / n))
            else:
                dirnames[:] = sorted(n for n in dirnames if not self._excluded_rel(join(n), True))
            for name in sorted(filenames):
                if not include_ignored and drel is not None and self._excluded_rel(join(name), False):
                    continue
                yield d / name
                count += 1
                if count >= max_files:
                    return

    def git_files(self) -> list[str] | None:
        try:
            r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=self.root,
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return [x for x in r.stdout.splitlines() if x]

    # ---- reading --------------------------------------------------------
    def read_text(self, p: Path) -> TextFile:
        raw = p.read_bytes()
        if is_binary_bytes(raw):
            raise PathError(f"{self.rel(p)} is a binary file")
        tf = decode_text(raw)
        self.read_digests[p] = tf.digest
        return tf

    def check_fresh(self, p: Path) -> None:
        """Refuse to overwrite a file the agent has not read, or that changed since it was read."""
        if not p.exists():
            return
        seen = self.read_digests.get(p)
        if seen is None:
            raise StaleFileError(f"read {self.rel(p)} before modifying it")
        current = sha256(p.read_bytes())
        if current != seen:
            raise StaleFileError(f"{self.rel(p)} changed on disk since it was last read "
                                 "(possibly edited by the user); re-read it before editing")

    # ---- writing --------------------------------------------------------
    def record(self, p: Path, before: bytes | None, after: bytes | None, tool: str, agent: str = "main") -> ChangeRecord:
        rec = ChangeRecord(self._next_change, self.rel(p), before, after, tool, self.current_task, agent)
        self._next_change += 1
        self.changes.append(rec)
        return rec

    def write_bytes(self, p: Path, data: bytes, tool: str, agent: str = "main") -> ChangeRecord:
        before = p.read_bytes() if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.naga-tmp")
        tmp.write_bytes(data)
        if p.exists():
            os.chmod(tmp, p.stat().st_mode & 0o7777)
        os.replace(tmp, p)
        self.read_digests[p] = sha256(data)
        return self.record(p, before, data, tool, agent)

    def changed_files(self, task_id: str | None = None) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.changes:
            if task_id is None or c.task_id == task_id:
                seen[c.path] = None
        return list(seen)

    def undo_last(self) -> tuple[bool, str]:
        """Revert the most recent agent change if the file still matches what the agent wrote.

        If the user modified the file afterwards, nothing is overwritten; a reverse patch
        is returned for review instead.
        """
        if not self.changes:
            return False, "no recorded agent changes to undo"
        rec = self.changes[-1]
        p = self.root / rec.path
        current = p.read_bytes() if p.exists() else None
        if current != rec.after:
            from .tools.patching import unified_diff
            before_t = rec.before.decode(errors="replace") if rec.before is not None else ""
            cur_t = current.decode(errors="replace") if current is not None else ""
            patch = unified_diff(cur_t, before_t, rec.path)
            return False, (f"{rec.path} was modified after the agent's change; not overwriting it. "
                           f"Review this reverse patch instead:\n{patch}")
        if rec.before is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rec.before)
            self.read_digests[p] = sha256(rec.before)
        self.changes.pop()
        return True, f"reverted change #{rec.id}: {rec.summary()}"
