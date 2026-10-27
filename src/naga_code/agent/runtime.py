"""The controlled agent loop: model -> tool calls -> real results -> model, with hard limits."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable

from ..providers.base import GenerationOptions, Message, Provider, ProviderError, ToolCall, Usage
from ..providers.tool_protocol import describe_tools, parse_tool_calls
from ..tools.context import ToolContext
from ..tools.registry import TaskCancelled, ToolRegistry, ToolResult
from .context import ContextManager
from .prompts import REQUIREMENTS_CHECK, REQUIREMENTS_RUN, VALIDATION_NUDGE

VALIDATION_TOOLS = {"run_tests", "run_lint"}
EDITING_TOOLS = {"write_file", "edit_file", "apply_patch"}
REPEAT_NUDGE = ("[naga runtime] You already gave your final summary. If the task is complete, reply with the final "
                "answer only and NO tool calls. Only call a tool if something essential is still unverified.")
DENIAL_NUDGE = ("[naga runtime] {n} actions were denied by the permission system in this turn. Do not keep trying "
                "variants of denied actions. Finish with what you can verify and tell the user what was not done.")
TEST_WORDS = ("pytest", "test", "lint", "ruff", "mypy", "tsc", "eslint", "cargo check", "cargo build", "go vet",
              "go build", "make", "build", "unittest", "py_compile", "node --check")


@dataclass
class AgentEvent:
    kind: str   # turn_start | text | thinking | assistant | tool_call | tool_result | info | compaction | error | final
    text: str = ""
    call: ToolCall | None = None
    result: ToolResult | None = None
    usage: Usage | None = None
    agent: str = "main"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentLimits:
    max_iterations: int = 40
    max_wall_seconds: float = 1800
    max_repeat_failures: int = 2
    validation_nudge: bool = True
    requirements_check: bool = True   # before finishing a turn that changed files, check each stated requirement


@dataclass
class TurnResult:
    text: str
    stop_reason: str             # completed | iteration_limit | time_limit | cancelled | error | repeated_failures
    iterations: int
    tool_calls: int
    files_changed: list[str] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)


EventSink = Callable[[AgentEvent], None]

PROVIDER_RETRY = ("[naga runtime] The inference backend failed to parse your previous output ({error}). "
                  "Try again: emit tool calls in the correct format, one at a time, or answer in plain text.")


def _retryable(e: ProviderError) -> bool:
    """Malformed model output (backend tool-call parser errors) and transient 5xx are worth one retry."""
    t = str(e).lower()
    if "cannot connect" in t or "out of memory" in t or "not found" in t:
        return False
    return any(k in t for k in ("syntax error", "parse", "unexpected", "invalid character", "error 500", "error 502",
                                "error 503", "timed out", "unmarshal"))


class Agent:
    def __init__(self, provider: Provider, model: str, registry: ToolRegistry, ctx: ToolContext,
                 context: ContextManager, system_prompt: Callable[[], str], *, tool_mode: str = "native",
                 options: GenerationOptions | None = None, limits: AgentLimits | None = None,
                 on_event: EventSink | None = None, name: str = "main",
                 runtime_context: Callable[[], str] | None = None):
        self.provider = provider
        self.model = model
        self.registry = registry
        self.ctx = ctx
        self.context = context
        self.system_prompt = system_prompt
        self.tool_mode = tool_mode
        self.options = options or GenerationOptions()
        self.limits = limits or AgentLimits()
        self.on_event = on_event or (lambda e: None)
        self.name = name
        self.runtime_context = runtime_context
        self.history: list[Message] = []
        self.total_usage = Usage(0, 0, 0.0)
        self.call_log: list[dict[str, Any]] = []   # per model call: tokens and prompt-processing time
        self.last_estimate = 0

    def emit(self, kind: str, **kw: Any) -> None:
        self.on_event(AgentEvent(kind, agent=self.name, **kw))

    # ---- model call -----------------------------------------------------
    def build_messages(self) -> tuple[list[Message], list[dict] | None]:
        tools = self.registry.schemas()
        system = self.system_prompt()
        text_tools = self.tool_mode == "text"
        if text_tools and tools:
            system += "\n\n" + describe_tools(tools)
        msgs = [Message("system", system)] + self.context.prepare(self.history, text_tools)
        note = self.runtime_context() if self.runtime_context else ""
        if note:  # last, so everything before it stays a cacheable prefix
            msgs.append(Message("user", "[naga runtime note, not a new request]\n" + note, meta={"synthetic": True}))
        return msgs, (tools if tools and not text_tools else None)

    async def _call_model(self, msgs: list[Message], tools: list[dict] | None) -> tuple[str, list[ToolCall], Usage | None]:
        text: list[str] = []
        calls: list[ToolCall] = []
        usage = None
        async for ev in self.provider.stream_chat(self.model, msgs, tools, self.options):
            if ev.type == "text":
                text.append(ev.text)
                self.emit("text", text=ev.text)
            elif ev.type == "thinking":
                self.emit("thinking", text=ev.text)
            elif ev.type == "tool_call" and ev.tool_call:
                calls.append(ev.tool_call)
            elif ev.type == "done":
                usage = ev.usage
        if usage:
            self.call_log.append({"prompt_tokens": usage.prompt_tokens, "prompt_eval_s": usage.prompt_eval_s,
                                  "completion_tokens": usage.completion_tokens, "seconds": usage.duration_s})
            self.context.calibrate(sum(len(m.content) for m in msgs) + (len(json.dumps(tools)) if tools else 0),
                                   usage.prompt_tokens)
            self.total_usage.prompt_tokens = (self.total_usage.prompt_tokens or 0) + (usage.prompt_tokens or 0)
            self.total_usage.completion_tokens = (self.total_usage.completion_tokens or 0) + (usage.completion_tokens or 0)
            self.total_usage.duration_s = (self.total_usage.duration_s or 0) + (usage.duration_s or 0)
        return "".join(text), calls, usage

    async def compact(self, force: bool = False, focus: str | None = None) -> str:
        self.history, note = await self.context.compact(self.history, self.provider, self.model, self.options, force,
                                                        focus)
        self.emit("compaction", text=note)
        return note

    # ---- the loop -------------------------------------------------------
    async def run(self, user_input: str, images: list[str] | None = None) -> TurnResult:
        change_mark = len(self.ctx.workspace.changes)
        val_mark = len(self.ctx.task.validations) if self.ctx.task else 0
        meta: dict[str, Any] = {"change_mark": change_mark}   # lets /rewind restore files to this point
        if images:
            meta["images"] = images
        self.history.append(Message("user", user_input, meta=meta))
        self.emit("turn_start", text=user_input)
        if self.ctx.hooks:
            await self.ctx.hooks.run("before_planning", {"prompt": user_input[:2000]})
        t0 = time.monotonic()
        failures: Counter[str] = Counter()
        validated = nudged = denial_nudged = checked = False
        summaries: list[str] = []
        repeats = denials = provider_retries = 0
        n_calls = 0
        final_text = ""
        stop = "iteration_limit"
        it = 0

        def result(reason: str, text: str) -> TurnResult:
            files = list(dict.fromkeys(c.path for c in self.ctx.workspace.changes[change_mark:]))
            vals = self.ctx.task.validations[val_mark:] if self.ctx.task else []
            return TurnResult(text, reason, it, n_calls, files, vals)

        try:
            for it in range(1, self.limits.max_iterations + 1):
                if time.monotonic() - t0 > self.limits.max_wall_seconds:
                    stop = "time_limit"
                    break
                msgs, tools = self.build_messages()
                self.last_estimate = self.context.estimate(msgs, tools)
                if self.context.needs_compaction(self.last_estimate) and len(self.history) > 4:
                    await self.compact()
                    msgs, tools = self.build_messages()
                    self.last_estimate = self.context.estimate(msgs, tools)
                try:
                    text, calls, usage = await self._call_model(msgs, tools)
                except ProviderError as e:
                    if provider_retries < 2 and _retryable(e):
                        provider_retries += 1
                        self.history.append(Message("user", PROVIDER_RETRY.format(error=str(e)[:300]),
                                                    meta={"synthetic": True}))
                        self.emit("info", text=f"backend rejected the model output ({str(e)[:120]}); retrying")
                        continue
                    self.emit("error", text=str(e))
                    return result("error", f"Model error: {e}")
                raw = text
                if self.tool_mode == "text" or (not calls and ("<tool_call>" in text or "<function=" in text)):
                    text, calls = parse_tool_calls(text, self.registry.schemas())
                # Completion detection: some local models restate their final summary and keep tacking on
                # "verification" tool calls. Nudge once; the second time accept the summary as the answer.
                repeated = calls and len(text) > 150 and any(
                    SequenceMatcher(None, text[:600], p[:600]).ratio() > 0.75 for p in summaries)
                if len(text) > 150:
                    summaries.append(text)
                if repeated:
                    repeats += 1
                    if repeats >= 2:
                        self.history.append(Message("assistant", text, meta={"dropped_tool_calls": len(calls)}))
                        self.emit("assistant", text=text, usage=usage, data={"tool_calls": 0})
                        self.emit("info", text=f"model repeated its final summary; treating it as the answer and "
                                               f"skipping {len(calls)} extra tool call(s)")
                        final_text, stop = text, "completed"
                        break
                self.history.append(Message("assistant", text, calls, meta={"raw": raw} if raw != text else {}))
                self.emit("assistant", text=text, usage=usage, data={"tool_calls": len(calls)})
                if not calls:
                    edited = [c.path for c in self.ctx.workspace.changes[change_mark:]]
                    if (edited and not validated and not nudged and self.limits.validation_nudge
                            and self.ctx.permissions.agent_mode == "agent" and it < self.limits.max_iterations):
                        nudged = True
                        self.history.append(Message("user", VALIDATION_NUDGE.format(files=", ".join(dict.fromkeys(edited))),
                                                    meta={"synthetic": True}))
                        self.emit("info", text="no validation was run after edits; asking the model to validate")
                        continue
                    if checked == "pending":   # the model answered the check without running anything
                        checked = True
                        self.history.append(Message("user", REQUIREMENTS_RUN, meta={"synthetic": True}))
                        self.emit("info", text="requirements check: asking the model to actually run its checks")
                        continue
                    if (edited and not checked and self.limits.requirements_check
                            and self.ctx.permissions.agent_mode == "agent" and it < self.limits.max_iterations):
                        checked = "pending"   # becomes True once the model runs a tool after the check message
                        self.history.append(Message("user", REQUIREMENTS_CHECK.format(request=user_input[:4000]),
                                                    meta={"synthetic": True}))
                        self.emit("info", text="checking the result against each requirement in the request")
                        continue
                    final_text = text
                    stop = "completed"
                    break
                if checked == "pending":
                    checked = True
                stop_now = False
                sigs = [c.name + json.dumps(c.arguments, sort_keys=True) for c in calls]
                prefetched: dict[int, ToolResult] = {}
                if len(calls) > 1 and all(self._read_only(c) and failures[g] < self.limits.max_repeat_failures
                                          for c, g in zip(calls, sigs, strict=True)):
                    # Several reads/searches in one turn: run them concurrently (writes always stay sequential).
                    for call in calls:
                        self.emit("tool_call", call=call)
                    done = await asyncio.gather(*(self.registry.execute(c, self.ctx) for c in calls))
                    prefetched = dict(enumerate(done))
                for i, call in enumerate(calls):
                    n_calls += 1
                    sig = sigs[i]
                    if i in prefetched:
                        res = prefetched[i]
                        if not res.ok:
                            failures[sig] += 1
                    elif failures[sig] >= self.limits.max_repeat_failures:
                        self.emit("tool_call", call=call)
                        res = ToolResult.error(f"this exact call already failed {failures[sig]} times; it was not run "
                                               "again. Change your approach, or stop and explain the blocker.")
                        failures[sig] += 1
                        if failures[sig] > self.limits.max_repeat_failures + 1:
                            stop_now = True
                    else:
                        self.emit("tool_call", call=call)
                        res = await self.registry.execute(call, self.ctx)
                        if not res.ok:
                            failures[sig] += 1
                    if call.name in VALIDATION_TOOLS or (call.name == "run_command" and res.data.get("exit_code") is not None
                                                         and any(w in call.arguments.get("command", "") for w in TEST_WORDS)):
                        validated = True
                    if res.output.startswith("Error: permission denied"):
                        denials += 1
                    if res.ok and call.name in EDITING_TOOLS:
                        res = self._with_code_check(res)
                        if self.ctx.services.get("ide") is not None:
                            res = await self._with_editor_errors(res, call)
                    self.history.append(Message("tool", res.output, tool_call_id=call.id, name=call.name,
                                                meta={"ok": res.ok, "summary": res.summary}))
                    self.emit("tool_result", call=call, result=res)
                if repeated:
                    self.history.append(Message("user", REPEAT_NUDGE, meta={"synthetic": True}))
                if denials >= 3 and not denial_nudged:
                    denial_nudged = True
                    self.history.append(Message("user", DENIAL_NUDGE.format(n=denials), meta={"synthetic": True}))
                if stop_now:
                    stop = "repeated_failures"
                    final_text = "Stopped: the agent kept repeating a failing action."
                    break
            else:
                stop = "iteration_limit"
        except (asyncio.CancelledError, TaskCancelled) as e:
            self._close_dangling_calls("cancelled by user")
            self.history.append(Message("user", "[naga runtime] The user cancelled the previous task.",
                                        meta={"synthetic": True}))
            self.emit("info", text="task cancelled")
            if isinstance(e, asyncio.CancelledError):
                raise
            return result("cancelled", "Task cancelled.")
        if stop in {"iteration_limit", "time_limit"}:
            final_text = (f"Stopped: reached the {'iteration' if stop == 'iteration_limit' else 'time'} limit before "
                          "the task was finished. Work so far is recorded; ask me to continue.")
            self.emit("info", text=final_text)
        if self.ctx.hooks:
            await self.ctx.hooks.run("before_final_response", {"stop_reason": stop})
        res = result(stop, final_text)
        self.emit("final", text=final_text, data={"stop_reason": stop, "files": res.files_changed,
                                                   "validations": res.validations, "iterations": it})
        return res

    def _with_code_check(self, res: ToolResult) -> ToolResult:
        """Append syntax errors / undefined names in the files just edited (caught before any test runs)."""
        from ..codeintel import check_after_edit
        paths = list(dict.fromkeys(c.path for c in self.ctx.workspace.changes[-5:] if c.after is not None))
        try:
            report = check_after_edit(self.ctx.workspace.root, paths)
        except Exception:  # noqa: BLE001 - a best-effort check must never break the edit
            return res
        if report:
            res.output += f"\n\nAutomatic check of the edited file(s) found problems:\n{report}"
        return res

    async def _with_editor_errors(self, res: ToolResult, call: ToolCall) -> ToolResult:
        """Append the editor's errors/warnings for the files just edited (language servers see what tests miss)."""
        from ..ide import errors_after_edit
        paths = [p for p in dict.fromkeys(c.path for c in self.ctx.workspace.changes[-5:])]
        try:
            report = await errors_after_edit(self.ctx.services["ide"], self.ctx.workspace.root, paths,
                                             float(self.ctx.config.get("ide.diagnostics_delay_s", 1.0)))
        except Exception:  # noqa: BLE001 - the editor bridge is optional
            return res
        if report:
            res.output += f"\n\nThe editor now reports for the edited file(s):\n{report}"
        return res

    def _read_only(self, call: ToolCall) -> bool:
        from ..security.commands import Risk
        tool = self.registry.get(call.name)
        return tool is not None and tool.level == Risk.READ and not call.parse_error and not call.name.startswith("mcp__")

    def _close_dangling_calls(self, why: str) -> None:
        """Ensure every assistant tool call has a matching tool message (valid history after cancel)."""
        answered = {m.tool_call_id for m in self.history if m.role == "tool"}
        for m in list(self.history):
            if m.role == "assistant":
                for c in m.tool_calls:
                    if c.id not in answered:
                        self.history.append(Message("tool", f"Error: not executed ({why})", tool_call_id=c.id,
                                                    name=c.name, meta={"ok": False}))
