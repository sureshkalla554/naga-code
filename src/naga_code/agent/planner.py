"""Planning tools: structured plans, step status tracking and scratch notes (agent state only)."""

from __future__ import annotations

from typing import Any

from ..tools.context import ToolContext
from ..tools.registry import Tool, ToolRegistry, ToolResult
from .task_state import STEP_STATES, Plan, PlanStep

S = {"type": "string"}
LIST = {"type": "array", "items": S}


async def update_plan(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    assert ctx.task is not None
    old = ctx.task.plan
    steps = []
    for s in a.get("steps", []):
        st = s if isinstance(s, dict) else {"title": str(s)}
        steps.append(PlanStep(st["title"], st.get("status", "pending"), st.get("note", "")))
    ctx.task.plan = Plan(
        objective=a.get("objective", old.objective),
        acceptance_criteria=a.get("acceptance_criteria", old.acceptance_criteria),
        steps=steps or old.steps,
        files_likely_to_change=a.get("files_likely_to_change", old.files_likely_to_change),
        risks=a.get("risks", old.risks),
        validation_commands=a.get("validation_commands", old.validation_commands),
        dependencies=a.get("dependencies", old.dependencies),
        approved=old.approved and not steps,
    )
    ctx.notify("plan", ctx.task.plan.render())
    return ToolResult(True, "plan recorded:\n" + ctx.task.plan.render(), summary=f"plan with {len(ctx.task.plan.steps)} steps")


async def update_step(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    assert ctx.task is not None
    steps = ctx.task.plan.steps
    i = a["step"] - 1
    if not 0 <= i < len(steps):
        return ToolResult.error(f"step {a['step']} does not exist (plan has {len(steps)} steps)")
    steps[i].status = a["status"]
    if a.get("note"):
        steps[i].note = a["note"]
    ctx.notify("plan", ctx.task.plan.render())
    return ToolResult(True, f"step {a['step']} -> {a['status']}", summary=f"step {a['step']} {a['status']}")


async def add_note(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    assert ctx.task is not None
    ctx.task.notes.append(a["note"])
    return ToolResult(True, f"noted ({len(ctx.task.notes)} notes)", summary="note saved")


def register(reg: ToolRegistry) -> None:
    step = {"type": "object", "properties": {"title": S, "status": {"enum": list(STEP_STATES)}, "note": S},
            "required": ["title"], "additionalProperties": False}
    reg.register(Tool("update_plan", "Record or replace the structured task plan: objective, acceptance criteria, "
                                     "ordered steps, files likely to change, risks, validation commands.",
                      {"type": "object", "additionalProperties": False, "required": ["objective", "steps"],
                       "properties": {"objective": S, "acceptance_criteria": LIST, "steps": {"type": "array", "items": step},
                                      "files_likely_to_change": LIST, "risks": LIST, "validation_commands": LIST,
                                      "dependencies": LIST}},
                      update_plan, category="planning"))
    reg.register(Tool("update_step", "Set a plan step's status (pending, running, completed, blocked, failed) when "
                                     "real work on it starts or finishes.",
                      {"type": "object", "additionalProperties": False, "required": ["step", "status"],
                       "properties": {"step": {"type": "integer", "minimum": 1}, "status": {"enum": list(STEP_STATES)},
                                      "note": S}}, update_step, category="planning"))
    reg.register(Tool("add_note", "Save a short task-specific scratch note (kept in the session, not persistent memory).",
                      {"type": "object", "additionalProperties": False, "required": ["note"], "properties": {"note": S}},
                      add_note, category="planning"))
