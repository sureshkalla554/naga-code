"""Command execution tools. All of them route through the permission engine via assess()."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..security.commands import Risk, assess_command
from ..security.permissions import ApprovalRequest, command_grant_key
from ..terminal import CommandResult
from .context import ToolContext
from .registry import Tool, ToolRegistry, ToolResult


def _p(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def command_request(ctx: ToolContext, tool: str, command: str, cwd: Path, reason: str = "",
                    floor: Risk = Risk.READ) -> ApprovalRequest:
    a = assess_command(command, ctx.workspace.root, deny_prefixes=ctx.permissions.deny_commands)
    level = max(a.risk, floor)
    return ApprovalRequest(tool=tool, operation=command, level=level, cwd=str(cwd), reason=reason,
                           impact=a.describe(), network=a.network, outside_project=a.outside_project,
                           elevated=a.elevated, grant_key=command_grant_key(command), editable=True)


def render_result(ctx: ToolContext, res: CommandResult, label: str) -> str:
    limit = int(ctx.config.get("context.max_tool_output_chars", 12000))
    full = res.render()
    if len(full) <= limit:
        return full
    aid = ctx.artifacts.save(full, label)
    return res.render(max_chars=limit) + (f"\n[full output saved as artifact '{aid}' "
                                          f"({len(full)} chars); use read_output to search or page through it]")


def _cwd(ctx: ToolContext, a: dict[str, Any]) -> Path:
    cwd = ctx.workspace.resolve(a.get("cwd", "."))
    if not cwd.is_dir():
        raise ValueError(f"cwd {a.get('cwd')} is not a directory")
    return cwd


async def execute_command(ctx: ToolContext, command: str, cwd: Path, timeout: float | None,
                          kind: str = "command") -> CommandResult:
    if ctx.hooks and kind == "test":
        await ctx.hooks.run("before_tests", {"command": command})
    res = await ctx.terminal.run(command, cwd=cwd, timeout=timeout)
    if ctx.task:
        ctx.task.record_command(command, res.exit_code, res.duration_s, kind, res.timed_out)
    if ctx.hooks and kind == "test":
        await ctx.hooks.run("after_tests", {"command": command, "exit_code": res.exit_code})
    return res


VALIDATION_COMMAND = re.compile(r"\b(pytest|unittest|tox|nox|ruff|mypy|pyright|flake8|eslint|tsc|jest|vitest|"
                                r"cargo (test|check|clippy|build)|go (test|vet|build)|dotnet (test|build)|ctest|"
                                r"make (test|check|lint)|npm (test|run (test|lint|build))|pnpm (test|lint)|yarn (test|lint))\b")


async def run_command(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    res = await execute_command(ctx, a["command"], _cwd(ctx, a), a.get("timeout"))
    if ctx.task and VALIDATION_COMMAND.search(a["command"]):
        from .testing import summarize_test_output
        kind = "test" if re.search(r"test|pytest|jest|vitest|ctest", a["command"]) else "lint"
        verdict = "PASSED" if res.ok else ("TIMED OUT" if res.timed_out else "FAILED")
        ctx.task.record_validation(kind, a["command"], verdict, summarize_test_output(res.stdout + "\n" + res.stderr))
    return ToolResult(res.ok, render_result(ctx, res, a["command"]),
                      data={"exit_code": res.exit_code, "timed_out": res.timed_out},
                      summary=f"exit {res.exit_code}" + (" (timed out)" if res.timed_out else "") +
                              f" in {res.duration_s:.1f}s")


async def start_process(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    bp = await ctx.terminal.start(a["command"], cwd=_cwd(ctx, a))
    if ctx.task:
        ctx.task.record_command(a["command"], None, 0.0, "background")
    return ToolResult(True, f"started background process #{bp.id} (pid {bp.proc.pid}): {a['command']}\n"
                            "Use poll_process to read output and stop_process to stop it.",
                      data={"id": bp.id}, summary=f"started #{bp.id}")


async def poll_process(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    try:
        return ToolResult(True, ctx.terminal.poll(a["id"], a.get("max_lines", 200)), summary=f"polled #{a['id']}")
    except KeyError as e:
        return ToolResult.error(str(e))


async def stop_process(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    try:
        return ToolResult(True, await ctx.terminal.stop(a["id"]), summary=f"stopped #{a['id']}")
    except KeyError as e:
        return ToolResult.error(str(e))


async def list_processes(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = [f"#{b.id} [{b.status}] {b.command}" for b in ctx.terminal.background.values()]
    return ToolResult(True, "\n".join(rows) or "(no background processes)", summary=f"{len(rows)} processes")


async def read_output(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    try:
        text = ctx.artifacts.read(a["id"], a.get("offset", 0), a.get("limit", 200), a.get("pattern"))
    except KeyError as e:
        return ToolResult.error(str(e))
    return ToolResult(True, text, summary=f"artifact {a['id']}")


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    timeout = {"type": "number", "minimum": 1, "maximum": 1800}
    reg.register(Tool(
        "run_command", "Run a non-interactive shell command in the project (bash -c). stdin is closed; use flags "
                       "like --yes/-y or non-interactive modes. Provide 'reason' so the user can judge the request.",
        _p({"command": S, "cwd": S, "timeout": timeout, "reason": S}, ["command"]), run_command, Risk.EXECUTE,
        timeout=1900, category="terminal",
        assess=lambda ctx, a: command_request(ctx, "run_command", a["command"], _cwd(ctx, a), a.get("reason", ""))))
    reg.register(Tool(
        "start_process", "Start a long-running background process (dev server, watcher). Returns an id.",
        _p({"command": S, "cwd": S, "reason": S}, ["command"]), start_process, Risk.EXECUTE, category="terminal",
        assess=lambda ctx, a: command_request(ctx, "start_process", a["command"], _cwd(ctx, a), a.get("reason", ""),
                                              floor=Risk.EXECUTE)))
    reg.register(Tool("poll_process", "Read new output from a background process.",
                      _p({"id": {"type": "integer"}, "max_lines": {"type": "integer", "minimum": 1}}, ["id"]),
                      poll_process, category="terminal"))
    reg.register(Tool("stop_process", "Stop a background process started by the agent.",
                      _p({"id": {"type": "integer"}}, ["id"]), stop_process, category="terminal"))
    reg.register(Tool("list_processes", "List background processes started by the agent.", _p({}, []),
                      list_processes, category="terminal"))
    reg.register(Tool("read_output", "Page through or grep a large saved tool output (artifact id like 'out-3').",
                      _p({"id": S, "offset": {"type": "integer", "minimum": 0},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 1000}, "pattern": S}, ["id"]),
                      read_output, category="terminal"))
