"""Filesystem and code-editing tools. Every path goes through Workspace.resolve()."""

from __future__ import annotations

import fnmatch
import os
import stat as stat_mod
import time
from pathlib import Path
from typing import Any

from ..security.permissions import ApprovalRequest, Level
from ..workspace import PathError, StaleFileError, decode_text, is_binary_bytes, sha256
from .context import ToolContext
from .patching import PatchError, apply_hunks, diff_stats, parse_patch, unified_diff
from .registry import Tool, ToolRegistry, ToolResult


def _p(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ---------------------------------------------------------------- reading
async def list_directory(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    root = ctx.workspace.resolve(a.get("path", "."))
    if not root.is_dir():
        return ToolResult.error(f"{a.get('path', '.')} is not a directory")
    depth = a.get("depth", 1)
    lines: list[str] = []
    limit = 400

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            lines.append("  " * level + "[permission denied]")
            return
        for e in entries:
            if len(lines) >= limit:
                return
            if not a.get("show_hidden") and e.name.startswith(".") and e.name not in {".github", ".naga"}:
                continue
            if ctx.workspace.is_excluded(e):
                continue
            if e.is_symlink():
                lines.append("  " * level + f"{e.name}@ -> {os.readlink(e)}")
            elif e.is_dir():
                lines.append("  " * level + e.name + "/")
                if level + 1 < depth:
                    walk(e, level + 1)
            else:
                lines.append("  " * level + f"{e.name}  ({e.stat().st_size} B)")

    walk(root, 0)
    more = "\n... (truncated)" if len(lines) >= limit else ""
    return ToolResult(True, "\n".join(lines) + more or "(empty)", summary=f"{len(lines)} entries")


async def read_file(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = ctx.workspace.resolve(a["path"])
    if not p.is_file():
        return ToolResult.error(f"{a['path']} does not exist or is not a regular file")
    size = p.stat().st_size
    if size > 20 * 1024 * 1024:
        return ToolResult.error(f"{a['path']} is {size // 1024 // 1024} MB; use search_code or a line range")
    tf = ctx.workspace.read_text(p)
    lines = tf.text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    max_lines = int(ctx.config.get("context.max_file_read_lines", 2000))
    start = max(1, a.get("start_line", 1))
    end = min(total, a.get("end_line", start + max_lines - 1), start + max_lines - 1)
    body = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
    note = ""
    if end < total or start > 1:
        note = f"\n[showing lines {start}-{end} of {total}; request other ranges with start_line/end_line]"
    header = f"{ctx.workspace.rel(p)} ({total} lines, {tf.encoding}, {'CRLF' if tf.newline == chr(13) + chr(10) else 'LF'})\n"
    return ToolResult(True, header + (body or "(empty file)") + note, summary=f"{ctx.workspace.rel(p)} lines {start}-{end}/{total}")


async def file_info(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = ctx.workspace.resolve(a["path"])
    raw_path = ctx.workspace.root / a["path"] if not os.path.isabs(a["path"]) else Path(a["path"])
    if not p.exists():
        return ToolResult.error(f"{a['path']} does not exist")
    st = p.stat()
    info = {"path": ctx.workspace.rel(p), "type": "directory" if p.is_dir() else "file",
            "size_bytes": st.st_size, "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "mode": stat_mod.filemode(st.st_mode), "symlink": raw_path.is_symlink(),
            "ignored": ctx.workspace.is_excluded(p)}
    if p.is_file():
        with p.open("rb") as f:
            head = f.read(65536)
        info["binary"] = is_binary_bytes(head)
        if not info["binary"]:
            tf = decode_text(head)
            info.update(encoding=tf.encoding, line_endings="CRLF" if tf.newline == "\r\n" else "LF")
    return ToolResult(True, "\n".join(f"{k}: {v}" for k, v in info.items()), data=info, summary=info["path"])


async def find_files(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    base = ctx.workspace.resolve(a.get("path", "."))
    pattern = a["pattern"]
    limit = a.get("max_results", 200)
    out = []
    for fp in ctx.workspace.walk(base, include_ignored=a.get("include_ignored", False)):
        rel = ctx.workspace.rel(fp)
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fp.name, pattern) or \
                ("**/" in pattern and fnmatch.fnmatch(rel, pattern.replace("**/", ""))):
            out.append(rel)
            if len(out) >= limit:
                break
    more = f"\n... (stopped at {limit} results)" if len(out) >= limit else ""
    return ToolResult(True, "\n".join(out) + more if out else "(no matching files)", summary=f"{len(out)} files")


async def find_large_files(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    min_bytes = int(a.get("min_kb", 512)) * 1024
    hits = []
    for fp in ctx.workspace.walk(include_ignored=a.get("include_ignored", False)):
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size >= min_bytes:
            hits.append((size, ctx.workspace.rel(fp)))
    hits.sort(reverse=True)
    lines = [f"{s / 1024:>10.1f} KB  {r}" for s, r in hits[:100]]
    return ToolResult(True, "\n".join(lines) or "(none)", summary=f"{len(hits)} files >= {min_bytes // 1024} KB")


async def repo_summary(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from ..agent.repomap import build_repo_map
    text = build_repo_map(ctx.workspace, max_chars=a.get("max_chars", 6000))
    return ToolResult(True, text, summary="repository map")


# ---------------------------------------------------------------- writing
def _plan_write(ctx: ToolContext, a: dict[str, Any]) -> tuple[Path, bytes, str, str]:
    p = ctx.workspace.resolve(a["path"], write=True)
    rel = ctx.workspace.rel(p)
    if p.is_dir():
        raise PathError(f"{rel} is a directory")
    if p.exists():
        if not a.get("overwrite"):
            raise PathError(f"{rel} already exists; use edit_file/apply_patch, or overwrite=true after reading it")
        ctx.workspace.check_fresh(p)
        tf = decode_text(p.read_bytes())
        new = tf.encode(a["content"])
        return p, new, unified_diff(tf.text, a["content"], rel), f"overwrite {rel}"
    return p, a["content"].encode("utf-8"), unified_diff("", a["content"], rel), f"create {rel}"


def _plan_edit(ctx: ToolContext, a: dict[str, Any]) -> tuple[Path, bytes, str, str]:
    p = ctx.workspace.resolve(a["path"], write=True)
    rel = ctx.workspace.rel(p)
    if not p.is_file():
        raise PathError(f"{rel} does not exist (use write_file to create it)")
    ctx.workspace.check_fresh(p)
    raw = p.read_bytes()
    if is_binary_bytes(raw):
        raise PathError(f"{rel} is binary; refusing to edit")
    tf = decode_text(raw)
    old, new = a["old_string"].replace("\r\n", "\n"), a["new_string"].replace("\r\n", "\n")
    if old == new:
        raise PathError("old_string and new_string are identical; nothing to change")
    count = tf.text.count(old)
    if count == 0:
        stripped = [ln.strip() for ln in old.strip().splitlines() if ln.strip()]
        hint = ""
        if stripped and stripped[0] in tf.text:
            line_no = tf.text[:tf.text.index(stripped[0])].count("\n") + 1
            hint = f" (its first line appears near line {line_no}; check whitespace/indentation)"
        raise PathError(f"old_string not found in {rel}{hint}. Re-read the file and copy the text exactly.")
    if count > 1 and not a.get("replace_all"):
        raise PathError(f"old_string occurs {count} times in {rel}; add surrounding context or set replace_all")
    updated = tf.text.replace(old, new) if a.get("replace_all") else tf.text.replace(old, new, 1)
    return p, tf.encode(updated), unified_diff(tf.text, updated, rel), f"edit {rel} ({count if a.get('replace_all') else 1} replacement)"


def _plan_patch(ctx: ToolContext, a: dict[str, Any]) -> list[tuple[Path, bytes | None, str]]:
    plans: list[tuple[Path, bytes | None, str]] = []
    for fp in parse_patch(a["patch"]):
        target = fp.new_path or fp.old_path
        assert target
        p = ctx.workspace.resolve(target, write=True)
        rel = ctx.workspace.rel(p)
        if fp.old_path is None:  # new file
            if p.exists():
                raise PatchError(f"patch creates {rel}, but it already exists")
            content = "".join(ln + "\n" for h in fp.hunks for ln in h.new)
            plans.append((p, content.encode(), unified_diff("", content, rel)))
            continue
        src = ctx.workspace.resolve(fp.old_path, write=True)
        if not src.is_file():
            raise PatchError(f"patch modifies {fp.old_path}, which does not exist")
        ctx.workspace.check_fresh(src)
        tf = decode_text(src.read_bytes())
        if fp.new_path is None:
            plans.append((src, None, unified_diff(tf.text, "", rel)))
            continue
        new_text = apply_hunks(tf.text, fp.hunks, rel)
        if src != p:
            if p.exists():
                raise PatchError(f"patch renames to {rel}, which already exists")
            plans.append((src, None, f"rename {ctx.workspace.rel(src)} -> {rel}\n"))
        plans.append((p, tf.encode(new_text), unified_diff(tf.text, new_text, rel)))
    return plans


def _outside(ctx: ToolContext, *paths: Path) -> bool:
    """True for paths in a directory added with /add-dir: those edits are always asked, never auto/session-approved."""
    root = ctx.workspace.root
    return any(p != root and root not in p.parents for p in paths)


def _edit_request(ctx: ToolContext, tool: str, op: str, diff: str, outside: bool = False) -> ApprovalRequest:
    add, rem = diff_stats(diff)
    return ApprovalRequest(tool=tool, operation=op, level=Level.WRITE, cwd=str(ctx.workspace.root),
                           impact=f"+{add} -{rem} lines " + ("in an added directory outside the project" if outside
                                                             else "inside the project"), preview=diff[:6000],
                           grant_key="workspace-file-edits", outside_project=outside)


async def _write(ctx: ToolContext, p: Path, data: bytes | None, tool: str) -> None:
    rel = ctx.workspace.rel(p)
    if ctx.hooks:
        blocked = await ctx.hooks.run("before_file_modification", {"path": rel, "tool": tool})
        if blocked:
            raise PathError(f"blocked by hook: {blocked}")
    if data is None:
        before = p.read_bytes()
        p.unlink()
        ctx.workspace.record(p, before, None, tool, ctx.agent_name)
    else:
        ctx.workspace.write_bytes(p, data, tool, ctx.agent_name)
    if ctx.hooks:
        await ctx.hooks.run("after_file_modification", {"path": rel, "tool": tool})


def _verify(p: Path, expected: bytes | None) -> str:
    actual = p.read_bytes() if p.exists() else None
    return "verified on disk" if actual == expected else "WARNING: file content differs from what was written (a hook may have reformatted it)"


async def write_file(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        p, data, diff, op = _plan_write(ctx, a)
        await _write(ctx, p, data, "write_file")
        add, rem = diff_stats(diff)
        return ToolResult(True, f"{op}: +{add} -{rem} lines ({_verify(p, data)})", diff=diff,
                          summary=f"{op} (+{add} -{rem})", data={"path": ctx.workspace.rel(p)})


async def edit_file(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        p, data, diff, op = _plan_edit(ctx, a)
        await _write(ctx, p, data, "edit_file")
        add, rem = diff_stats(diff)
        return ToolResult(True, f"{op}: +{add} -{rem} lines ({_verify(p, data)})\n{diff[:3000]}", diff=diff,
                          summary=f"{op} (+{add} -{rem})", data={"path": ctx.workspace.rel(p)})


async def apply_patch(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        plans = _plan_patch(ctx, a)  # every hunk validated before anything is written
        for p, data, _ in plans:
            await _write(ctx, p, data, "apply_patch")
        diff = "".join(d for _, _, d in plans)
        add, rem = diff_stats(diff)
        files = sorted({ctx.workspace.rel(p) for p, _, _ in plans})
        return ToolResult(True, f"applied patch to {len(files)} file(s): {', '.join(files)} (+{add} -{rem})",
                          diff=diff, summary=f"patched {len(files)} file(s) (+{add} -{rem})", data={"files": files})


async def move_path(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        src = ctx.workspace.resolve(a["source"], write=True)
        dst = ctx.workspace.resolve(a["destination"], write=True)
        if not src.exists():
            return ToolResult.error(f"{a['source']} does not exist")
        if dst.exists():
            return ToolResult.error(f"{a['destination']} already exists; refusing to overwrite")
        if src.is_dir():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return ToolResult(True, f"moved directory {ctx.workspace.rel(src)} -> {ctx.workspace.rel(dst)} "
                                    "(directory moves are not recorded for /undo)", summary="moved directory")
        data = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        ctx.workspace.record(src, data, None, "move_path", ctx.agent_name)
        ctx.workspace.record(dst, None, data, "move_path", ctx.agent_name)
        ctx.workspace.read_digests.pop(src, None)
        ctx.workspace.read_digests[dst] = sha256(data)
        return ToolResult(True, f"moved {ctx.workspace.rel(src)} -> {ctx.workspace.rel(dst)}", summary="moved file")


def _delete_target(ctx: ToolContext, a: dict[str, Any]) -> Path:
    p = ctx.workspace.resolve(a["path"], write=True)
    if not p.is_file() or p.is_symlink():
        raise PathError(f"{a['path']} is not a regular file (only single files can be deleted)")
    return p


async def delete_file(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        p = _delete_target(ctx, a)
        await _write(ctx, p, None, "delete_file")
        ctx.workspace.read_digests.pop(p, None)
        return ToolResult(True, f"deleted {ctx.workspace.rel(p)} (recoverable with undo_last_change)",
                          summary=f"deleted {ctx.workspace.rel(p)}")


def _assess_delete(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    p = _delete_target(ctx, a)
    created = any(c.path == ctx.workspace.rel(p) and c.before is None for c in ctx.workspace.changes)
    return ApprovalRequest("delete_file", f"delete {ctx.workspace.rel(p)}", Level.WRITE, str(ctx.workspace.root),
                           impact=("file was created by the agent in this session" if created else
                                   "deletes a pre-existing file (restorable with /undo while unchanged)"),
                           grant_key="workspace-file-edits" if created else None, outside_project=_outside(ctx, p))


async def make_directory(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = ctx.workspace.resolve(a["path"], write=True)
    p.mkdir(parents=True, exist_ok=True)
    return ToolResult(True, f"directory {ctx.workspace.rel(p)} exists", summary=f"mkdir {ctx.workspace.rel(p)}")


async def undo_last_change(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    async with ctx.workspace.write_lock:
        ok, msg = ctx.workspace.undo_last()
    return ToolResult(ok, msg, summary=msg.splitlines()[0])


def _assess_write(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    p, _, diff, op = _plan_write(ctx, a)
    return _edit_request(ctx, "write_file", op, diff, _outside(ctx, p))


def _assess_edit(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    p, _, diff, op = _plan_edit(ctx, a)
    return _edit_request(ctx, "edit_file", op, diff, _outside(ctx, p))


def _assess_patch(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    plans = _plan_patch(ctx, a)
    files = sorted({ctx.workspace.rel(p) for p, _, _ in plans})
    return _edit_request(ctx, "apply_patch", f"apply patch to {', '.join(files)}", "".join(d for _, _, d in plans),
                         _outside(ctx, *(p for p, _, _ in plans)))


def _assess_move(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    src = ctx.workspace.resolve(a["source"], write=True)
    dst = ctx.workspace.resolve(a["destination"], write=True)
    return ApprovalRequest("move_path", f"move {ctx.workspace.rel(src)} -> {ctx.workspace.rel(dst)}", Level.WRITE,
                           str(ctx.workspace.root), grant_key="workspace-file-edits",
                           outside_project=_outside(ctx, src, dst))


def _assess_mkdir(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    p = ctx.workspace.resolve(a["path"], write=True)
    return ApprovalRequest("make_directory", f"mkdir -p {ctx.workspace.rel(p)}", Level.WRITE, str(ctx.workspace.root),
                           grant_key="workspace-file-edits", outside_project=_outside(ctx, p))


def _assess_undo(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
    last = ctx.workspace.changes[-1].summary() if ctx.workspace.changes else "nothing"
    return ApprovalRequest("undo_last_change", f"revert last agent change ({last})", Level.WRITE,
                           str(ctx.workspace.root), grant_key="workspace-file-edits")


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    reg.register(Tool("list_directory", "List a directory inside the project (respects .gitignore and exclusions).",
                      _p({"path": S, "depth": {"type": "integer", "minimum": 1, "maximum": 4},
                          "show_hidden": {"type": "boolean"}}, []), list_directory, category="filesystem"))
    reg.register(Tool("read_file", "Read a text file with line numbers. Use start_line/end_line for large files. "
                                   "Always read a file before editing it.",
                      _p({"path": S, "start_line": {"type": "integer", "minimum": 1},
                          "end_line": {"type": "integer", "minimum": 1}}, ["path"]), read_file, category="filesystem"))
    reg.register(Tool("file_info", "Metadata for a path: size, mtime, binary, encoding, line endings, symlink.",
                      _p({"path": S}, ["path"]), file_info, category="filesystem"))
    reg.register(Tool("find_files", "Find files by glob (e.g. '*.py', 'src/**/test_*.ts').",
                      _p({"pattern": S, "path": S, "max_results": {"type": "integer", "minimum": 1, "maximum": 2000},
                          "include_ignored": {"type": "boolean"}}, ["pattern"]), find_files, category="filesystem"))
    reg.register(Tool("find_large_files", "List files at or above a size threshold (KB).",
                      _p({"min_kb": {"type": "integer", "minimum": 1}, "include_ignored": {"type": "boolean"}}, []),
                      find_large_files, category="filesystem"))
    reg.register(Tool("repo_summary", "Compact map of the repository: layout, manifests, languages, tests, git state.",
                      _p({"max_chars": {"type": "integer", "minimum": 500, "maximum": 30000}}, []), repo_summary,
                      category="filesystem"))
    reg.register(Tool("write_file", "Create a new file. To replace an existing file entirely set overwrite=true "
                                    "(only after reading it); prefer edit_file or apply_patch for changes.",
                      _p({"path": S, "content": S, "overwrite": {"type": "boolean"}}, ["path", "content"]), write_file,
                      Level.WRITE, category="editing", assess=_assess_write))
    reg.register(Tool("edit_file", "Replace an exact text span in a file. old_string must match the file exactly "
                                   "(including indentation) and be unique unless replace_all=true.",
                      _p({"path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}},
                         ["path", "old_string", "new_string"]), edit_file, Level.WRITE, category="editing",
                      assess=_assess_edit))
    reg.register(Tool("apply_patch", "Apply a unified diff (--- a/path, +++ b/path, @@ hunks) to one or more files. "
                                     "All hunks are validated before any file is written.",
                      _p({"patch": S}, ["patch"]), apply_patch, Level.WRITE, category="editing", assess=_assess_patch))
    reg.register(Tool("move_path", "Rename or move a file or directory inside the project (never overwrites).",
                      _p({"source": S, "destination": S}, ["source", "destination"]), move_path, Level.WRITE,
                      category="editing", assess=_assess_move))
    reg.register(Tool("delete_file", "Delete a single file inside the project, e.g. temporary/debug files you created. "
                                     "Recorded in the change ledger so it can be undone.",
                      _p({"path": S}, ["path"]), delete_file, Level.WRITE, category="editing", assess=_assess_delete))
    reg.register(Tool("make_directory", "Create a directory (and parents) inside the project.",
                      _p({"path": S}, ["path"]), make_directory, Level.WRITE, category="editing", assess=_assess_mkdir))
    reg.register(Tool("undo_last_change", "Revert the most recent file change made by the agent, only if the file "
                                          "was not modified since.", _p({}, []), undo_last_change, Level.WRITE,
                      category="editing", assess=_assess_undo))


__all__ = ["register", "PathError", "StaleFileError"]
