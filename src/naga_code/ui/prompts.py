"""Interactive approval prompt."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit import PromptSession
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ..security.commands import Risk
from ..security.permissions import ApprovalRequest, ApprovalResponse, Choice
from . import style as ui_style
from .select import Option, select


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path

LEVEL_STYLE = {Risk.READ: "green", Risk.WRITE: "yellow", Risk.EXECUTE: "cyan", Risk.DANGEROUS: "bold red"}


class ApprovalPrompter:
    def __init__(self, console: Console, before: callable | None = None, after: callable | None = None):
        self.console = console
        self.session: PromptSession = PromptSession()
        self.before = before  # e.g. stop live rendering
        self.after = after    # e.g. restore the Ctrl+C task-cancel handler (prompt-toolkit removes it)

    async def __call__(self, req: ApprovalRequest) -> ApprovalResponse:
        try:
            return await self._ask(req)
        finally:
            if self.after:
                self.after()

    async def _ask(self, req: ApprovalRequest) -> ApprovalResponse:
        if self.before:
            self.before()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        t.add_row("operation", req.operation)
        t.add_row("requested by", req.agent)
        t.add_row("working dir", _tilde(req.cwd) if req.cwd else "-")
        if req.reason:
            t.add_row("reason", req.reason)
        t.add_row("impact", req.impact or "-")
        t.add_row("network", "[yellow]yes[/]" if req.network else "no")
        t.add_row("outside project", "[red]yes[/]" if req.outside_project else "no")
        t.add_row("elevated", "[red]yes[/]" if req.elevated else "no")
        parts = [t]
        if req.preview:
            lines = req.preview.splitlines()
            parts.append(Syntax("\n".join(lines[:60]) + (f"\n… {len(lines) - 60} more lines" if len(lines) > 60 else ""),
                                "diff", theme=ui_style.STATE["syntax"], word_wrap=True, background_color="default"))
        style = LEVEL_STYLE.get(req.level, "white")
        self.console.print(Panel(Group(*parts), title=f"[{style}]approval needed · {req.level.name.lower()} · {req.tool}[/]",
                                 border_style=style))
        grantable = req.grant_key is not None and req.level < Risk.DANGEROUS and not req.outside_project
        is_cmd = req.tool == "run_command"
        opts = [Option("once", "Yes" + (", run it" if is_cmd else ", allow this once"), "y")]
        if grantable:
            scope = ("file edits in this project" if req.grant_key == "workspace-file-edits"
                     else f"'{req.grant_key}' commands" if is_cmd else f"'{req.grant_key}'")
            opts.append(Option("session", f"Yes, and don't ask again for {scope} this session", "a"))
        opts.append(Option("deny", "No, and tell the agent what to do instead", "n"))
        if req.editable:
            opts.append(Option("edit", "Edit the command first", "e"))
        opts.append(Option("cancel", "Cancel the whole task", "c"))
        while True:
            try:
                ans = await select(opts, cancel_value="cancel")
            except (KeyboardInterrupt, EOFError):
                ans = "cancel"
            label = next((o.label for o in opts if o.value == ans), "")
            self.console.print(f"  [dim]→ {label}[/]")
            if ans == "once":
                return ApprovalResponse(Choice.APPROVE_ONCE)
            if ans == "session":
                return ApprovalResponse(Choice.APPROVE_SESSION)
            if ans == "deny":
                try:
                    note = (await self.session.prompt_async("  reason for the agent (optional, enter to skip)> ")).strip()
                except (KeyboardInterrupt, EOFError):
                    note = ""
                return ApprovalResponse(Choice.DENY, note=note or None)
            if ans == "edit":
                try:
                    edited = await self.session.prompt_async("  command> ", default=req.operation)
                except (KeyboardInterrupt, EOFError):
                    continue
                if edited.strip():
                    return ApprovalResponse(Choice.EDIT, edited=edited.strip())
                continue
            return ApprovalResponse(Choice.CANCEL)
