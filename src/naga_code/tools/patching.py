"""Unified diff generation and a tolerant-but-safe unified diff applier."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


def unified_diff(old: str, new: str, path: str, context: int = 3) -> str:
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n\\ No newline at end of file\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n\\ No newline at end of file\n"
    return "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=context))


def diff_stats(diff: str) -> tuple[int, int]:
    add = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return add, rem


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # each prefixed with ' ', '-', '+'

    @property
    def old(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[:1] in (" ", "-")]

    @property
    def new(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[:1] in (" ", "+")]


@dataclass
class FilePatch:
    old_path: str | None   # None = new file
    new_path: str | None   # None = deleted file
    hunks: list[Hunk] = field(default_factory=list)


def _strip_prefix(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FilePatch(_strip_prefix(ln[4:]), _strip_prefix(lines[i + 1][4:]))
            files.append(cur)
            hunk = None
            i += 2
            continue
        m = _HUNK.match(ln)
        if m:
            if cur is None:
                raise PatchError("hunk header before any '---/+++' file header")
            hunk = Hunk(old_start=int(m.group(1)))
            cur.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None and ln[:1] in (" ", "-", "+"):
            hunk.lines.append(ln)
        elif hunk is not None and ln == "":
            hunk.lines.append(" ")  # some generators strip the space on blank context lines
        elif ln.startswith("\\"):
            pass  # "\ No newline at end of file"
        i += 1
    if not files:
        raise PatchError("no file headers ('--- a/path' / '+++ b/path') found in patch")
    for f in files:
        if not f.hunks and f.old_path is not None and f.new_path is not None and f.old_path == f.new_path:
            raise PatchError(f"patch for {f.old_path} contains no hunks")
    return files


def _find(haystack: list[str], needle: list[str], hint: int, loose: bool) -> int:
    def eq(a: str, b: str) -> bool:
        return a.rstrip() == b.rstrip() if loose else a == b

    if not needle:
        return min(max(hint, 0), len(haystack))
    order = sorted(range(len(haystack) - len(needle) + 1), key=lambda k: abs(k - hint))
    for k in order:
        if all(eq(haystack[k + j], needle[j]) for j in range(len(needle))):
            return k
    return -1


def apply_hunks(text: str, hunks: list[Hunk], path: str = "") -> str:
    """Apply hunks to text. Context must match (trailing whitespace tolerated); no fuzzy content matching."""
    trailing_nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if trailing_nl:
        lines = lines[:-1] if lines and lines[-1] == "" else lines
    offset = 0
    for n, h in enumerate(hunks, 1):
        hint = h.old_start - 1 + offset
        pos = _find(lines, h.old, hint, loose=False)
        if pos < 0:
            pos = _find(lines, h.old, hint, loose=True)
        if pos < 0:
            preview = "\n".join(h.old[:6])
            raise PatchError(f"hunk {n} for {path or 'file'} does not match the current file content "
                             f"(expected near line {h.old_start}):\n{preview}\nRe-read the file and regenerate the patch.")
        lines[pos:pos + len(h.old)] = h.new
        # pos already includes the shift from earlier hunks; add this hunk's size change.
        offset = pos - (h.old_start - 1) + len(h.new) - len(h.old)
    out = "\n".join(lines)
    return out + "\n" if (trailing_nl and lines) else out
