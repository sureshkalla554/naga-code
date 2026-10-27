"""Tools that let the agent load skills progressively."""

from __future__ import annotations

from typing import Any

from ..tools.context import ToolContext
from ..tools.registry import Tool, ToolRegistry, ToolResult
from . import SkillLibrary


def _lib(ctx: ToolContext) -> SkillLibrary:
    return ctx.services["skills"]


async def load_skill(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    lib = _lib(ctx)
    s = lib.get(a["name"])
    if not s:
        return ToolResult.error(f"no skill named '{a['name']}'. Known: {', '.join(lib.skills) or 'none'}")
    ctx.notify("skill", f"loaded skill '{s.name}' ({s.source})")
    return ToolResult(True, lib.render(s, a.get("arguments", "")), summary=f"skill {s.name}")


async def read_skill_file(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    lib = _lib(ctx)
    s = lib.get(a["name"])
    if not s:
        return ToolResult.error(f"no skill named '{a['name']}'")
    try:
        return ToolResult(True, lib.read_file(s, a["path"]), summary=f"{s.name}/{a['path']}")
    except ValueError as e:
        return ToolResult.error(str(e))


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    reg.register(Tool("load_skill", "Load a skill's full instructions by name (see the Skills index).",
                      {"type": "object", "properties": {"name": S, "arguments": S}, "required": ["name"],
                       "additionalProperties": False}, load_skill, category="skills"))
    reg.register(Tool("read_skill_file", "Read a reference/template file bundled with a skill.",
                      {"type": "object", "properties": {"name": S, "path": S}, "required": ["name", "path"],
                       "additionalProperties": False}, read_skill_file, category="skills"))
