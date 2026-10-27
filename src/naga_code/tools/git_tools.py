"""Git tools. Destructive operations are not exposed; commits/branches always need approval."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .. import git as G
from ..security.permissions import ApprovalRequest, Level
from .context import ToolContext
from .registry import Tool, ToolRegistry, ToolResult

REF = re.compile(r"^[A-Za-z0-9._/@{}~^-]+$")


def _obj(props: dict[str, Any], req: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


async def _git(ctx: ToolContext, *args: str) -> tuple[int, str]:
    r = await asyncio.to_thread(G.git, ctx.workspace.root, *args)
    return r.returncode, (r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")).strip()


def _ref(v: str) -> str:
    if not REF.match(v) or v.startswith("-"):
        raise ValueError(f"invalid git ref '{v}'")
    return v


def _paths(ctx: ToolContext, paths: list[str]) -> list[str]:
    return [ctx.workspace.rel(ctx.workspace.resolve(p)) for p in paths]


def _limit(ctx: ToolContext, text: str, label: str) -> str:
    limit = int(ctx.config.get("context.max_tool_output_chars", 12000))
    if len(text) <= limit:
        return text
    aid = ctx.artifacts.save(text, label)
    return text[:limit] + f"\n... [truncated; full output in artifact '{aid}', use read_output]"


async def git_status(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    st = await asyncio.to_thread(G.status, ctx.workspace.root)
    if not st.is_repo:
        return ToolResult.error("not a git repository")
    return ToolResult(True, st.short() + "\n" + st.raw, data={"branch": st.branch}, summary=st.short())


async def git_diff(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    args = ["diff", "--no-color"]
    if a.get("staged"):
        args.append("--cached")
    if a.get("stat_only"):
        args.append("--stat")
    if a.get("base"):
        args.append(_ref(a["base"]))
    args.append("--")
    args += _paths(ctx, a.get("paths", []))
    code, out = await _git(ctx, *args)
    if code != 0:
        return ToolResult.error(out)
    return ToolResult(True, _limit(ctx, out, "git diff") or "(no differences)", summary="diff")


async def git_log(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    args = ["log", f"-n{a.get('limit', 20)}", "--date=short", "--pretty=format:%h %ad %an  %s"]
    if a.get("ref"):
        args.append(_ref(a["ref"]))
    if a.get("paths"):
        args += ["--", *_paths(ctx, a["paths"])]
    code, out = await _git(ctx, *args)
    return ToolResult(code == 0, out or "(no commits)", summary="log")


async def git_show(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    args = ["show", "--no-color", _ref(a.get("ref", "HEAD"))]
    if a.get("stat_only"):
        args.append("--stat")
    code, out = await _git(ctx, *args)
    return ToolResult(code == 0, _limit(ctx, out, "git show"), summary=f"show {a.get('ref', 'HEAD')}")


async def git_branches(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    code, out = await _git(ctx, "branch", "-a", "-vv", "--no-color")
    return ToolResult(code == 0, out or "(no branches)", summary="branches")


async def git_compare(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    base, head = _ref(a["base"]), _ref(a.get("head", "HEAD"))
    _, commits = await _git(ctx, "log", "--oneline", f"{base}..{head}")
    _, stat = await _git(ctx, "diff", "--stat", f"{base}...{head}")
    return ToolResult(True, f"commits in {head} not in {base}:\n{commits or '(none)'}\n\nchanged files:\n{stat}",
                      summary=f"{base}...{head}")


async def git_stage(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    code, out = await _git(ctx, "add", "--", *_paths(ctx, a["paths"]))
    return ToolResult(code == 0, out or f"staged {len(a['paths'])} path(s)", summary="staged")


async def git_commit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    code, staged = await _git(ctx, "diff", "--cached", "--name-only")
    if not staged.strip():
        return ToolResult.error("nothing is staged; stage files with git_stage first")
    code, out = await _git(ctx, "commit", "-m", a["message"])
    return ToolResult(code == 0, out, summary="committed" if code == 0 else "commit failed")


async def git_create_branch(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    args = ["switch", "-c", _ref(a["name"])] if a.get("switch", True) else ["branch", _ref(a["name"])]
    code, out = await _git(ctx, *args)
    return ToolResult(code == 0, out or f"created branch {a['name']}", summary=f"branch {a['name']}")


def _req(op: str, level: Level, grant: str | None = None):
    def assess(ctx: ToolContext, a: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest("git", op.format(**{k: (" ".join(v) if isinstance(v, list) else v) for k, v in a.items()}),
                               level, str(ctx.workspace.root), grant_key=grant)
    return assess


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    paths = {"type": "array", "items": S}
    reg.register(Tool("git_status", "Show branch and working-tree status.", _obj({}, []), git_status, category="git"))
    reg.register(Tool("git_diff", "Show unstaged (default) or staged changes, optionally vs a base ref or for paths.",
                      _obj({"staged": {"type": "boolean"}, "stat_only": {"type": "boolean"}, "base": S, "paths": paths}, []),
                      git_diff, category="git"))
    reg.register(Tool("git_log", "Show recent commit history.", _obj({"limit": {"type": "integer", "minimum": 1,
                                                                                "maximum": 200}, "ref": S, "paths": paths}, []),
                      git_log, category="git"))
    reg.register(Tool("git_show", "Show a commit.", _obj({"ref": S, "stat_only": {"type": "boolean"}}, []), git_show,
                      category="git"))
    reg.register(Tool("git_branches", "List branches.", _obj({}, []), git_branches, category="git"))
    reg.register(Tool("git_compare", "Compare two refs: commits and changed files.", _obj({"base": S, "head": S}, ["base"]),
                      git_compare, category="git"))
    reg.register(Tool("git_stage", "Stage specific files (git add -- <paths>).", _obj({"paths": paths}, ["paths"]),
                      git_stage, Level.WRITE, category="git", assess=_req("git add -- {paths}", Level.WRITE, "git-stage")))
    reg.register(Tool("git_commit", "Create a commit from staged changes. Always requires user approval.",
                      _obj({"message": S}, ["message"]), git_commit, Level.EXECUTE, category="git",
                      assess=_req("git commit -m {message!r}", Level.EXECUTE)))
    reg.register(Tool("git_create_branch", "Create (and by default switch to) a new branch. Requires approval.",
                      _obj({"name": S, "switch": {"type": "boolean"}}, ["name"]), git_create_branch, Level.EXECUTE,
                      category="git", assess=_req("git switch -c {name}", Level.EXECUTE)))
