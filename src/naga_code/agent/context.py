"""Context engine: token estimation, tool-output elision and conversation compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..providers.base import GenerationOptions, Message, Provider
from ..providers.tool_protocol import format_tool_result

SUMMARY_PROMPT = """You are compacting a coding-agent conversation so work can continue in a smaller context.
Write a precise summary that a fresh agent can continue from. Keep, verbatim where possible:
1. Every user request and requirement, including acceptance criteria and constraints ("must", "don't").
2. Decisions made and why.
3. Files read, created or modified (with paths) and what changed.
4. Commands run and their REAL outcomes (pass/fail, error messages). Never upgrade a failure to a success.
5. Unresolved errors, open questions, and what remains to do next.
Do not invent anything that is not in the transcript. Use terse bullet points."""


@dataclass
class ContextBudget:
    context_length: int
    reserve_tokens: int = 4096
    compact_threshold: float = 0.75

    @property
    def usable(self) -> int:
        return max(self.context_length - self.reserve_tokens, 1024)


class ContextManager:
    def __init__(self, budget: ContextBudget, keep_recent_tool_results: int = 8, max_tool_output_chars: int = 12000):
        self.budget = budget
        self.keep_recent = keep_recent_tool_results
        self.max_tool_chars = max_tool_output_chars
        self.chars_per_token = 3.6
        self.last_prompt_tokens: int | None = None
        self.compactions = 0

    # ---- estimation -----------------------------------------------------
    def estimate_text(self, text: str) -> int:
        return int(len(text) / self.chars_per_token) + 1

    def estimate(self, messages: list[Message], tools: list[dict] | None = None) -> int:
        chars = sum(len(m.content) + 8 + sum(len(json.dumps(c.arguments)) + len(c.name) for c in m.tool_calls)
                    for m in messages)
        if tools:
            chars += len(json.dumps(tools))
        return int(chars / self.chars_per_token) + 4 * len(messages)

    def calibrate(self, prompt_chars: int, prompt_tokens: int | None) -> None:
        """Adjust the chars/token ratio from the backend's real prompt token count."""
        if not prompt_tokens or prompt_tokens < 200:
            return
        self.last_prompt_tokens = prompt_tokens
        ratio = prompt_chars / prompt_tokens
        if 1.5 < ratio < 8:
            self.chars_per_token = 0.7 * self.chars_per_token + 0.3 * ratio

    def utilization(self, tokens: int) -> float:
        return tokens / self.budget.context_length if self.budget.context_length else 0.0

    def needs_compaction(self, tokens: int) -> bool:
        return tokens > self.budget.usable * self.budget.compact_threshold

    # ---- preparation ----------------------------------------------------
    def prepare(self, history: list[Message], text_tools: bool = False) -> list[Message]:
        """Copy of history for the model: old tool outputs elided, text-tool encoding if needed.

        The stored history is never modified, so /export and resume keep full detail.
        """
        tool_idx = [i for i, m in enumerate(history) if m.role == "tool"]
        elide = set(tool_idx[:-self.keep_recent]) if self.keep_recent and len(tool_idx) > self.keep_recent else set()
        out: list[Message] = []
        for i, m in enumerate(history):
            content = m.content
            if m.role == "tool":
                if i in elide and len(content) > 400:
                    content = (content[:300] + f"\n[... older output elided ({len(content)} chars); re-run or "
                               "re-read if still needed]")
                elif len(content) > self.max_tool_chars:
                    content = content[:self.max_tool_chars] + "\n[... truncated]"
            if text_tools:
                if m.role == "tool":
                    out.append(Message("user", format_tool_result(m.name or "tool", content)))
                    continue
                if m.role == "assistant":
                    out.append(Message("assistant", m.meta.get("raw", m.content)))
                    continue
            out.append(Message(m.role, content, m.tool_calls, m.tool_call_id, m.name, m.meta))
        return out

    # ---- compaction -----------------------------------------------------
    @staticmethod
    def _cut_index(history: list[Message], keep_tokens: int, est) -> int:
        """Index where the kept tail starts: a user message, keeping roughly keep_tokens."""
        total = 0
        cut = len(history)
        for i in range(len(history) - 1, -1, -1):
            total += est([history[i]])
            if history[i].role == "user" and not history[i].meta.get("synthetic"):
                if total > keep_tokens:
                    break
                cut = i
        return cut

    @staticmethod
    def transcript(messages: list[Message], max_chars: int = 60000) -> str:
        lines = []
        for m in messages:
            if m.role == "tool":
                lines.append(f"[tool result: {m.name}] {m.content[:1500]}")
            elif m.role == "assistant":
                calls = "; ".join(f"{c.name}({json.dumps(c.arguments)[:300]})" for c in m.tool_calls)
                lines.append(f"[assistant] {m.content[:3000]}" + (f"\n  [tool calls] {calls}" if calls else ""))
            else:
                lines.append(f"[{m.role}] {m.content[:4000]}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars // 3] + "\n...\n" + text[-2 * max_chars // 3:]

    @staticmethod
    def fallback_summary(messages: list[Message]) -> str:
        users = [m.content[:500] for m in messages if m.role == "user" and not m.meta.get("synthetic")]
        files, errors = [], []
        for m in messages:
            for c in m.tool_calls:
                if c.name in {"write_file", "edit_file", "apply_patch"}:
                    files.append(c.arguments.get("path", "(patch)"))
            if m.role == "tool" and m.content.startswith("Error"):
                errors.append(m.content[:200])
        return ("User requests:\n" + "\n".join(f"- {u}" for u in users) +
                ("\nFiles touched: " + ", ".join(dict.fromkeys(files)) if files else "") +
                ("\nRecent errors:\n" + "\n".join(f"- {e}" for e in errors[-5:]) if errors else ""))

    async def compact(self, history: list[Message], provider: Provider, model: str,
                      options: GenerationOptions | None = None, force: bool = False,
                      focus: str | None = None) -> tuple[list[Message], str]:
        keep_tokens = int(self.budget.usable * 0.25)
        cut = self._cut_index(history, keep_tokens, self.estimate)
        if cut <= 1 and not force:
            return history, "nothing to compact"
        if cut <= 0:
            cut = len(history)
        head, tail = history[:cut], history[cut:]
        prompt = [Message("system", SUMMARY_PROMPT + (f"\n\nThe user asked the summary to focus on: {focus}" if focus else "")),
                  Message("user", "Transcript to summarize:\n\n" + self.transcript(head))]
        try:
            summary = (await provider.complete(model, prompt, options)).strip()
        except Exception as e:  # noqa: BLE001 - fall back to a deterministic summary
            summary = ""
            note = f" (model summary failed: {e}; used deterministic summary)"
        else:
            note = ""
        if not summary:
            summary = self.fallback_summary(head)
        self.compactions += 1
        new = [Message("user", "[Summary of the earlier conversation — verify against files before relying on it]\n"
                       + summary, meta={"summary": True, "synthetic": True}),
               Message("assistant", "Understood. I will continue from this summary and re-read files before editing.",
                       meta={"synthetic": True})] + tail
        return new, f"compacted {len(head)} messages into a summary{note}"
