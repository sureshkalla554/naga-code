"""Typed tool registry and the single execution path every tool call goes through.

validate arguments -> classify -> before-hook -> permission check -> run with timeout
-> audit -> after-hook. Nothing reaches a handler without passing every step.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import jsonschema

from ..providers.base import ToolCall
from ..security.permissions import ApprovalRequest, Level

if TYPE_CHECKING:
    from .context import ToolContext


class TaskCancelled(Exception):
    """Raised when the user cancels the whole task (e.g. from an approval prompt)."""


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""          # one-line description for the UI
    diff: str | None = None    # file edits attach their diff for review
    duration_s: float = 0.0

    @classmethod
    def error(cls, msg: str, **data: Any) -> "ToolResult":
        return cls(False, f"Error: {msg}", data, summary=msg.splitlines()[0][:120] if msg else "error")


Handler = Callable[["ToolContext", dict[str, Any]], Awaitable[ToolResult]]
Assessor = Callable[["ToolContext", dict[str, Any]], ApprovalRequest]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    level: Level = Level.READ
    timeout: float = 60.0
    category: str = "general"
    assess: Assessor | None = None
    network: bool = False
    output_format: str = "text"

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool '{tool.name}' already registered")
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list(self) -> list[Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def subset(self, names: list[str] | None = None, max_level: Level | None = None) -> "ToolRegistry":
        sub = ToolRegistry()
        for t in self._tools.values():
            if names is not None and t.name not in names and not any(
                    n.endswith("*") and t.name.startswith(n[:-1]) for n in names):
                continue
            if max_level is not None and t.level > max_level:
                continue
            sub._tools[t.name] = t
        return sub

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.list()]

    @staticmethod
    def validate(tool: Tool, args: dict[str, Any]) -> str | None:
        v = jsonschema.Draft202012Validator(tool.parameters)
        errors = sorted(v.iter_errors(args), key=lambda e: list(e.path))
        if not errors:
            return None
        return "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5])

    def _default_request(self, tool: Tool, ctx: "ToolContext", args: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(tool=tool.name, level=tool.level, cwd=str(ctx.workspace.root),
                               operation=f"{tool.name} {json.dumps(args, ensure_ascii=False)[:800]}",
                               network=tool.network, agent=ctx.agent_name,
                               grant_key=tool.name if tool.level < Level.DANGEROUS else None)

    async def execute(self, call: ToolCall, ctx: "ToolContext") -> ToolResult:
        t0 = time.monotonic()
        if call.parse_error:
            return ToolResult.error(f"tool call was NOT executed: {call.parse_error}. Emit a well-formed call.")
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.error(f"unknown tool '{call.name}'. Available tools: {', '.join(self.names())}")
        args = dict(call.arguments)
        problem = self.validate(tool, args)
        if problem:
            return ToolResult.error(f"invalid arguments for {tool.name}: {problem}. Tool was not executed.")

        for _attempt in range(3):
            try:
                req = tool.assess(ctx, args) if tool.assess else self._default_request(tool, ctx, args)
            except Exception as e:  # assessment errors are argument errors (bad path, ...)
                return ToolResult.error(f"{tool.name}: {e}")
            req.agent = ctx.agent_name
            if ctx.hooks:
                blocked = await ctx.hooks.run("before_tool", {"tool": tool.name, "arguments": args,
                                                               "level": req.level.name})
                if blocked:
                    return ToolResult.error(f"blocked by hook: {blocked}")
            auth = await ctx.permissions.authorize(req)
            if auth.cancelled:
                raise TaskCancelled(auth.reason)
            if auth.edited is not None and "command" in args:
                args["command"] = auth.edited
                ctx.notify("info", f"command edited by user: {auth.edited}")
                continue
            if not auth.allowed:
                if ctx.audit:
                    ctx.audit.record("tool_denied", tool=tool.name, arguments=args, reason=auth.reason)
                return ToolResult.error(f"permission denied for {tool.name}: {auth.reason}. Do not retry the same "
                                        "action; choose a different approach or ask the user.")
            break
        else:
            return ToolResult.error("too many edits to the command; not executed")

        try:
            result = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout)
        except asyncio.TimeoutError:
            result = ToolResult.error(f"{tool.name} timed out after {tool.timeout:.0f}s")
        except (TaskCancelled, asyncio.CancelledError):
            raise
        except Exception as e:  # tool bugs and OS errors become model-visible errors
            result = ToolResult.error(f"{tool.name} failed: {e.__class__.__name__}: {e}")
        result.duration_s = time.monotonic() - t0
        if ctx.audit:
            ctx.audit.record("tool", tool=tool.name, arguments=args, ok=result.ok, agent=ctx.agent_name,
                             duration_s=round(result.duration_s, 3), output_chars=len(result.output))
        if ctx.hooks:
            await ctx.hooks.run("after_tool", {"tool": tool.name, "arguments": args, "ok": result.ok})
        return result
