"""Subagent manager: isolated context, bounded tools/permissions, structured output, parallel read-only runs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..agent.context import ContextBudget, ContextManager
from ..agent.runtime import Agent, AgentEvent, AgentLimits
from ..agent.task_state import TaskState
from ..tools.context import ToolContext
from ..tools.registry import Tool, ToolRegistry, ToolResult
from .definitions import BUILTIN, SubagentDef, from_config

RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"}, "location": {"type": "string"},
            "severity": {"enum": ["critical", "high", "medium", "low", "info"]}}, "required": ["title"]}},
        "files_examined": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "validation": {"type": "string"},
        "confidence": {"enum": ["high", "medium", "low"]},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class SubagentResult:
    agent: str
    task: str
    ok: bool
    stop_reason: str
    structured: bool
    result: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    files_changed: list[str] = field(default_factory=list)

    def render(self) -> str:
        r = self.result
        lines = [f"## Subagent '{self.agent}' — {'completed' if self.ok else self.stop_reason} "
                 f"({self.duration_s:.0f}s, {'structured' if self.structured else 'unstructured'} result)",
                 f"Task: {self.task}", "", r.get("summary", "(no summary)")]
        for f in r.get("findings", []):
            lines.append(f"- [{f.get('severity', 'info')}] {f.get('title')}"
                         + (f" ({f['location']})" if f.get("location") else "") + (f": {f['detail']}" if f.get("detail") else ""))
        for key in ("files_examined", "open_questions"):
            if r.get(key):
                lines.append(f"{key.replace('_', ' ')}: " + ", ".join(r[key]))
        if self.files_changed:
            lines.append("files actually changed (from the change ledger): " + ", ".join(self.files_changed))
        if r.get("validation"):
            lines.append(f"validation (as reported by subagent): {r['validation']}")
        lines.append("Review these results before relying on them; verify key claims by reading the files.")
        return "\n".join(lines)


ModelFactory = Callable[[str], dict[str, Any]]  # profile -> {provider, model, options, tool_mode, context_length}


class SubagentManager:
    def __init__(self, registry: ToolRegistry, model_factory: ModelFactory, config_defs: dict[str, dict] | None = None,
                 on_event: Callable[[AgentEvent], None] | None = None):
        self.registry = registry
        self.model_factory = model_factory
        self.on_event = on_event or (lambda e: None)
        self.defs: dict[str, SubagentDef] = dict(BUILTIN)
        self.errors: list[str] = []
        for name, d in (config_defs or {}).items():
            try:
                self.defs[name] = from_config(name, d)
            except ValueError as e:
                self.errors.append(str(e))
        self.history: list[SubagentResult] = []

    def index_prompt(self) -> str:
        return ("# Subagents\nUse the delegate tool for bounded, independent sub-tasks (several read-only tasks run in "
                "parallel). Each runs with its own context and a restricted tool set; review its result.\n" +
                "\n".join(f"- {d.name}: {d.description}" for d in self.defs.values()))

    async def run(self, name: str, task: str, parent: ToolContext) -> SubagentResult:
        d = self.defs.get(name)
        if d is None:
            return SubagentResult(name, task, False, "unknown_agent", False,
                                  {"summary": f"unknown subagent '{name}'. Known: {', '.join(self.defs)}"})
        t0 = time.monotonic()
        submitted: dict[str, Any] = {}

        async def submit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
            submitted.update(a)
            return ToolResult(True, "result submitted; now reply with a one-line final message and stop.")

        sub_reg = self.registry.subset([t for t in d.tools if t != "delegate"], max_level=d.max_level)
        sub_reg.register(Tool("submit_result", "Submit your final structured result (call exactly once at the end).",
                              RESULT_SCHEMA, submit, category="subagent"), replace=True)
        label = f"sub:{d.name}"
        ctx = parent.derive(agent_name=label, permissions=parent.permissions.child(d.max_level, label),
                            task=TaskState())
        m = self.model_factory(d.profile)
        cm = ContextManager(ContextBudget(m["context_length"], 2048, 0.8), keep_recent_tool_results=6)
        system = (f"You are the '{d.name}' subagent of Naga Code, working for an orchestrating agent, not directly for "
                  f"the user.\n{d.instructions}\n\nRules: stay within the task; only use the tools you have; never "
                  "claim results you did not observe; when finished call submit_result exactly once with a concise "
                  f"summary and findings, then stop.\nProject root: {parent.workspace.root}")
        change_mark = len(parent.workspace.changes)

        def forward(e: AgentEvent) -> None:
            if e.kind in {"tool_call", "tool_result", "error", "info"}:
                self.on_event(e)

        agent = Agent(m["provider"], m["model"], sub_reg, ctx, cm, lambda: system, tool_mode=m["tool_mode"],
                      options=m["options"], limits=AgentLimits(d.max_iterations, d.timeout, 2, validation_nudge=False),
                      on_event=forward, name=label)
        try:
            turn = await asyncio.wait_for(agent.run(task), d.timeout)
            stop, text = turn.stop_reason, turn.text
        except asyncio.TimeoutError:
            stop, text = "timeout", f"subagent timed out after {d.timeout:.0f}s"
        changed = list(dict.fromkeys(c.path for c in parent.workspace.changes[change_mark:] if c.agent == label))
        structured = bool(submitted)
        result = submitted if structured else {"summary": text or "(subagent produced no output)"}
        res = SubagentResult(d.name, task, stop == "completed", stop, structured, result,
                             time.monotonic() - t0, changed)
        self.history.append(res)
        return res

    async def run_many(self, tasks: list[dict[str, str]], parent: ToolContext) -> list[SubagentResult]:
        """Read-only subagents run concurrently; writers run one at a time afterwards (no conflicting writes)."""
        readers = [t for t in tasks if self.defs.get(t["agent"]) is None or self.defs[t["agent"]].read_only]
        writers = [t for t in tasks if t not in readers]
        results: dict[int, SubagentResult] = {}
        rr = await asyncio.gather(*(self.run(t["agent"], t["task"], parent) for t in readers))
        for t, r in zip(readers, rr, strict=True):
            results[tasks.index(t)] = r
        for t in writers:
            results[tasks.index(t)] = await self.run(t["agent"], t["task"], parent)
        return [results[i] for i in range(len(tasks))]


async def delegate(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    mgr: SubagentManager = ctx.services["subagents"]
    if ctx.agent_name != "main":
        return ToolResult.error("subagents cannot delegate further")
    tasks = a.get("tasks") or ([{"agent": a["agent"], "task": a["task"]}] if a.get("agent") and a.get("task") else [])
    if not tasks:
        return ToolResult.error("provide 'agent' and 'task', or a 'tasks' list")
    if len(tasks) > 6:
        return ToolResult.error("at most 6 subagent tasks per call")
    ctx.notify("info", "delegating: " + "; ".join(f"{t['agent']}: {t['task'][:60]}" for t in tasks))
    results = await mgr.run_many(tasks, ctx)
    return ToolResult(all(r.ok for r in results), "\n\n".join(r.render() for r in results),
                      summary=f"{sum(r.ok for r in results)}/{len(results)} subagents completed")


def register(reg: ToolRegistry, agent_names: list[str]) -> None:
    task = {"type": "object", "properties": {"agent": {"enum": agent_names}, "task": {"type": "string"}},
            "required": ["agent", "task"], "additionalProperties": False}
    reg.register(Tool("delegate", "Delegate bounded sub-tasks to specialized subagents. Give each a complete, "
                                  "self-contained task description (they do not see this conversation).",
                      {"type": "object", "additionalProperties": False,
                       "properties": {"agent": {"enum": agent_names}, "task": {"type": "string"},
                                      "tasks": {"type": "array", "items": task, "maxItems": 6}}},
                      delegate, timeout=3600, category="subagents"), replace=True)
