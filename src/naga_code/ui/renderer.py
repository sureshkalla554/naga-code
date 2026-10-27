"""Renders agent events to the terminal: streaming Markdown, tool activity, diffs, plans, summaries."""

from __future__ import annotations

import json

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from ..agent.runtime import AgentEvent
from . import style

TOOL_ICON = {"filesystem": "📄", "editing": "✏️ ", "terminal": "⚡", "search": "🔎", "git": "⎇ ", "testing": "🧪",
             "planning": "🗒 ", "skills": "📘", "subagents": "🤖", "mcp": "🔌", "web": "🌐", "memory": "🧠"}


def short_args(args: dict, width: int = 90) -> str:
    if not args:
        return ""
    keys = ("command", "path", "pattern", "name", "query", "url", "agent", "objective", "id", "step", "ref")
    for k in keys:
        if k in args:
            v = str(args[k]).replace("\n", " ⏎ ")
            extra = ""
            if k == "path" and ("start_line" in args or "end_line" in args):
                extra = f":{args.get('start_line', 1)}-{args.get('end_line', '')}"
            return (v[:width] + "…" if len(v) > width else v) + extra
    s = json.dumps(args, ensure_ascii=False)
    return s[:width] + ("…" if len(s) > width else "")


class EventRenderer:
    def __init__(self, console: Console, compact: bool = False):
        self.console = console
        self.compact = compact
        self.verbose = False
        self.buffer = ""
        self.live: Live | None = None
        self.thinking_chars = 0
        self.categories: dict[str, str] = {}
        self.last_result = None

    def _stop_live(self) -> None:
        if self.live:
            self.live.update(Markdown(self.buffer))
            self.live.stop()
            self.live = None
        elif self.buffer.strip() and not self.console.is_terminal:
            self.console.print(Markdown(self.buffer))
        self.buffer = ""

    def stop(self) -> None:
        self._stop_live()

    def diff(self, diff: str, max_lines: int | None = 40) -> None:
        lines = diff.splitlines()
        shown = "\n".join(lines[:max_lines]) if max_lines else diff
        self.console.print(Syntax(shown, "diff", theme=style.STATE["syntax"], word_wrap=True, background_color="default"))
        if max_lines and len(lines) > max_lines:
            self.console.print(f"[dim]  … {len(lines) - max_lines} more diff lines (/last shows all)[/]")

    def handle(self, e: AgentEvent) -> None:
        prefix = "" if e.agent == "main" else f"[magenta]{e.agent}[/] "
        if e.kind == "text" and e.agent == "main":
            if self.thinking_chars:
                self.thinking_chars = 0
            self.buffer += e.text
            if not self.console.is_terminal:
                return  # no live redraw on pipes/files; printed once when the message completes
            if self.live is None:
                self.live = Live(Markdown(self.buffer), console=self.console, refresh_per_second=10,
                                 vertical_overflow="visible")
                self.live.start()
            else:
                self.live.update(Markdown(self.buffer))
        elif e.kind == "thinking" and e.agent == "main":
            self.thinking_chars += len(e.text)
            if self.verbose:
                self.console.print(Text(e.text, style="dim italic"), end="")
        elif e.kind == "assistant":
            self._stop_live()
        elif e.kind in {"tool_call", "tool_result"} and style.STATE["focus"] and not self.verbose and \
                (e.kind == "tool_call" or (e.result is not None and e.result.ok)):
            self._stop_live()  # focus view: tool activity hidden (failures still shown)
        elif e.kind == "tool_call" and e.call:
            self._stop_live()
            icon = TOOL_ICON.get(self.categories.get(e.call.name, ""), "•")
            label = e.call.name if e.call.name else "(malformed tool call)"
            self.console.print(f"{prefix}[bold cyan]{icon} {label}[/] [dim]{short_args(e.call.arguments)}[/]")
        elif e.kind == "tool_result" and e.result:
            r = e.result
            if e.agent == "main":
                self.last_result = r
            color = "green" if r.ok else "red"
            self.console.print(f"{prefix}  [dim]⎿[/] [{color}]{r.summary or ('ok' if r.ok else 'failed')}[/]"
                               f"[dim] ({r.duration_s:.1f}s)[/]")
            if not r.ok and not self.verbose:
                self.console.print(Text("    " + r.output.strip().replace("\n", "\n    ")[:600], style="red dim"))
            if r.diff and e.agent == "main" and not self.compact:
                self.diff(r.diff, 30)
            if self.verbose and r.ok:
                body = r.output if len(r.output) < 4000 else r.output[:4000] + "\n…"
                self.console.print(Panel(Text(body), border_style="dim", title=e.call.name if e.call else ""))
        elif e.kind == "info":
            self._stop_live()
            self.console.print(f"{prefix}[yellow]ℹ {e.text}[/]")
        elif e.kind == "compaction":
            self.console.print(f"[yellow]⟲ context {e.text}[/]")
        elif e.kind == "error":
            self._stop_live()
            self.console.print(Panel(e.text, title="error", border_style="red"))
        elif e.kind == "final" and e.agent == "main":
            self._stop_live()
            self.summary(e)

    def summary(self, e: AgentEvent) -> None:
        """Facts from the runtime's records (not the model's claims)."""
        files = e.data.get("files", [])
        vals = e.data.get("validations", [])
        stop = e.data.get("stop_reason", "")
        parts = []
        if files:
            parts.append(f"changed: {', '.join(files[:8])}" + (f" +{len(files) - 8}" if len(files) > 8 else ""))
        if vals:
            latest: dict[str, dict] = {}
            for v in vals:
                latest[v["command"]] = v
            for cmd, v in list(latest.items())[-4:]:
                color = "green" if v["verdict"] == "PASSED" else "red"
                parts.append(f"validation: `{cmd}` latest [{color}]{v['verdict']}[/]")
            failing = [c for c, v in latest.items() if v["verdict"] != "PASSED"]
            if failing:
                parts.append(f"[red]{len(failing)} validation command(s) not passing at their latest run[/]")
        elif files:
            parts.append("validation: [yellow]none recorded by run_tests/run_lint[/]")
        if stop != "completed":
            parts.append(f"[red]stopped: {stop}[/]")
        if parts:
            self.console.print(Panel(Group(*[Text.from_markup(p) for p in parts]), border_style="dim",
                                     title="turn summary (from runtime records)", title_align="left"))
