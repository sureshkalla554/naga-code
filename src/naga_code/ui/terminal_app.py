"""Interactive terminal session: prompt-toolkit input + Rich output."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .. import __version__
from .. import git as G
from ..app import MODES, NagaApp
from ..commands import REGISTRY, load_project_commands
from ..config import data_home
from ..providers.base import Message
from . import style
from .logo import tiger
from .prompts import ApprovalPrompter, _tilde
from .renderer import EventRenderer
from .select import Option, select


COMMAND_ARGS = {
    "/effort": ["low", "medium", "high", "xhigh", "max", "auto", "status"], "/fast": ["on", "off"],
    "/theme": list(style.THEMES), "/color": style.PROMPT_COLORS + ["default"], "/plugin": ["list", "install", "enable",
    "disable", "remove"], "/output-style": ["default", "concise", "explanatory", "learning"], "/goal": ["clear"],
    "/advisor": ["off"], "/autocompact": ["auto"], "/sandbox": ["auto", "bwrap", "none"], "/schedule": ["list", "add",
    "rm"], "/code-review": ["low", "medium", "high", "xhigh", "max", "ultra", "--fix"], "/context": ["all"],
    "/import": ["codex", "gemini", "cursor", "--dry-run", "--yes"], "/statusline": ["off"],
    "/custom-model": ["list", "use", "remove"],
}


class SlashCompleter(Completer):
    def __init__(self, app: NagaApp):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        if " " not in text:
            for name in REGISTRY.names(include_hidden=False):
                if name.startswith(text[1:]):
                    cmd = REGISTRY.resolve(name)
                    yield Completion("/" + name, start_position=-len(text), display_meta=(cmd.help[:60] if cmd else ""))
            return
        head, _, arg = text.partition(" ")
        options: list[str] = []
        if head in {"/skill"}:
            options = list(self.app.skills.skills)
        elif head in {"/model"}:
            options = list(self.app.config.section("profiles"))
        elif head in {"/mode"}:
            options = list(MODES)
        elif head in {"/delegate"}:
            options = list(self.app.subagents.defs)
        elif head == "/mcp":
            options = ["list", "connect", "disconnect", "reconnect", "enable", "disable", "trust", "tools",
                       "resources"] + list(self.app.mcp.servers)
        else:
            options = COMMAND_ARGS.get(head, [])
        for o in options:
            if o.startswith(arg.split(" ")[-1]) and " " not in arg:
                yield Completion(o, start_position=-len(arg))


class TerminalApp:
    def __init__(self, cwd: Path, *, overrides: dict | None = None, profile: str | None = None, model: str | None = None,
                 compact: bool | None = None, resume: str | None = None, mode: str | None = None):
        self.console = Console(highlight=False)
        compact = compact if compact is not None else self.console.width < 90
        self.renderer = EventRenderer(self.console, compact)
        self.prompter = ApprovalPrompter(self.console, before=self.renderer.stop, after=self._install_sigint)
        self._running: asyncio.Future | None = None
        self.app = NagaApp(cwd, overrides=overrides, profile=profile, approval_handler=self.prompter,
                           on_event=self.renderer.handle, notify=self._notify, model=model)
        self.compact = compact
        self.resume_id = resume
        self.initial_mode = mode
        self.task_status = "idle"
        self._toolbar_cache = ""
        self.prompt_session: PromptSession | None = None
        self.prefill = ""            # text put back into the input box (e.g. by /rewind)
        self._statusline = ("", 0.0)

    # ---- UI protocol used by commands --------------------------------------
    @property
    def verbose(self) -> bool:
        return self.renderer.verbose

    @verbose.setter
    def verbose(self, v: bool) -> None:
        self.renderer.verbose = v

    @property
    def last_result(self):
        return self.renderer.last_result

    @last_result.setter
    def last_result(self, r) -> None:
        self.renderer.last_result = r

    def info(self, msg: str) -> None:
        self.console.print(f"[blue]ℹ[/] {msg}")

    def warn(self, msg: str) -> None:
        self.console.print(f"[yellow]⚠ {msg}[/]")

    def error(self, msg: str) -> None:
        self.console.print(f"[red]✘ {msg}[/]")

    def success(self, msg: str) -> None:
        self.console.print(f"[green]✔[/] {msg}")

    def show_diff(self, diff: str) -> None:
        self.console.print(Syntax(diff, "diff", theme=style.STATE["syntax"], word_wrap=True, background_color="default"))

    async def confirm(self, question: str, default: bool = False) -> bool:
        self.renderer.stop()
        self.console.print(f"[bold]{question}[/]")
        opts = [Option("yes", "Yes", "y"), Option("no", "No", "n")]
        try:
            ans = await select(opts, cancel_value="no", default=0 if default else 1)
        except (KeyboardInterrupt, EOFError):
            ans = "no"
        finally:
            self._install_sigint()
        self.console.print(f"  [dim]→ {'Yes' if ans == 'yes' else 'No'}[/]")
        return ans == "yes"

    async def choose(self, question: str, options: list[tuple[str, str]], default: int = 0) -> str | None:
        self.renderer.stop()
        self.console.print(f"[bold]{question}[/]")
        try:
            ans = await select([Option(v, label) for v, label in options], default=default)
        except (KeyboardInterrupt, EOFError):
            ans = None
        finally:
            self._install_sigint()
        self.console.print(f"  [dim]→ {next((label for v, label in options if v == ans), 'cancelled')}[/]")
        return ans

    async def sleep(self, seconds: float) -> bool:
        """Wait, but let Ctrl+C cut it short (returns False if cancelled)."""
        task = asyncio.ensure_future(asyncio.sleep(seconds))
        self._running = task
        self._install_sigint()
        try:
            await task
            return True
        except asyncio.CancelledError:
            return False
        finally:
            self._running = None
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass

    async def change_dir(self, target: Path) -> None:
        """/cd: rebuild the app for another directory and carry the conversation over."""
        old = self.app
        old.save()
        history = list(old.agent.history) if old.agent else []
        new = NagaApp(target, profile=old.profile_name, approval_handler=self.prompter, on_event=self.renderer.handle,
                      notify=self._notify, model=old._model_override)
        with self.console.status(f"moving to {target}…"):
            await new.start()
        new.prompt_extras = dict(old.prompt_extras)
        new.goal = old.goal
        assert new.agent is not None
        new.agent.history = history
        new.session.messages = history
        new.session.title = old.session.title
        await old.shutdown()
        self.app = new
        load_project_commands(new.project_root)
        self.renderer.categories = {t.name: t.category for t in new.registry.list()}
        if self.prompt_session and isinstance(self.prompt_session.completer, SlashCompleter):
            self.prompt_session.completer.app = new
        self.success(f"now in {new.cwd} (project {new.project_root}); conversation kept in session {new.session.id}")

    def _load_ui_prefs(self) -> None:
        ui = self.app.config.section("ui")
        try:
            style.set_theme(ui.get("theme", "dark"))
        except ValueError:
            self.warn(f"unknown ui.theme {ui.get('theme')!r}")
        style.STATE["statusline"] = ui.get("statusline", "")
        out = ui.get("output_style", "default")
        from ..commands_extra import OUTPUT_STYLES
        if OUTPUT_STYLES.get(out):
            self.app.prompt_extras["style"] = OUTPUT_STYLES[out]

    def _install_sigint(self) -> None:
        """Ctrl+C cancels the running agent task (re-installed after every prompt-toolkit prompt)."""
        if self._running is None or self._running.done():
            return
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self._running.cancel)
        except (NotImplementedError, RuntimeError):
            pass

    def _notify(self, kind: str, msg: str) -> None:
        if kind == "plan":
            self.renderer.stop()
            self.console.print(Panel(msg, title="plan", border_style="blue"))
        elif kind == "warning":
            self.warn(msg)
        elif kind == "hook":
            self.console.print(f"[dim]⚙ hook {msg}[/]")
        elif kind == "skill":
            self.console.print(f"[blue]📘 {msg}[/]")
        else:
            self.console.print(f"[dim]{msg}[/]")

    # ---- header / toolbar ----------------------------------------------------
    def _refresh_toolbar(self) -> None:
        s = self.app.status()
        pct = 100 * s["context_used"] / max(s["context_total"], 1)
        branch = G.branch(self.app.project_root) or "-"
        ok = s["provider"][0]
        self._toolbar_cache = (f" <b>{s['mode']}</b> mode (⇧⇥) │ {'' if ok else '<style fg=\"red\">offline </style>'}"
                               f"{s['model']} │ ctx {pct:.0f}% of {s['context_total'] // 1000}k │ ⎇ {branch} │ "
                               f"perm: {s['permission_mode']} │ {self.task_status}")
        extra = self._statusline_text()
        if extra:
            from html import escape
            self._toolbar_cache += f" │ {escape(extra)}"

    def _statusline_text(self) -> str:
        cmd = style.STATE.get("statusline")
        if not cmd:
            return ""
        if self._statusline[0] and time.monotonic() - self._statusline[1] < 5:
            return self._statusline[0]
        try:
            r = subprocess.run(cmd, shell=True, cwd=self.app.project_root, capture_output=True, text=True, timeout=2)
            text = (r.stdout.strip().splitlines() or [""])[0][:80]
        except (subprocess.TimeoutExpired, OSError):
            text = "(status line timed out)"
        self._statusline = (text, time.monotonic())
        return text

    def toolbar(self):
        return HTML(self._toolbar_cache)

    def banner(self) -> None:
        s = self.app.status()
        ok, detail = s["provider"]
        if self.compact:
            self.console.print(f"[bold #f28a1e]Naga Code[/] v{__version__} · {s['model']} · "
                               f"{'[green]●[/]' if ok else '[red]●[/]'} · {s['mode']}")
            return
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        t.add_row("project", _tilde(s["project"]))
        t.add_row("model", f"{s['model']}  [dim](profile {s['profile']}, {s['tool_mode']} tool calls, "
                           f"ctx {s['context_total']:,})[/]")
        t.add_row("provider", ("[green]● connected[/] " if ok else "[red]● unavailable[/] ") + f"[dim]{detail}[/]")
        t.add_row("git", s["git"])
        t.add_row("mode", f"{s['mode']} · permissions {s['permission_mode']}")
        t.add_row("sandbox", f"[dim]{s['sandbox']}[/]")
        if self.app.instruction_files:
            t.add_row("instructions", ", ".join(str(f.path.name) + f" ({f.scope})" for f in self.app.instruction_files))
        head = Table.grid(padding=(0, 3))
        head.add_column(vertical="middle")
        head.add_column(vertical="middle")
        head.add_row(tiger(), Group(Text.from_markup(f"[bold #f28a1e]Naga Code[/] [dim]v{__version__}[/]\n"), t))
        self.console.print(Panel(head, subtitle="/help · local-first", border_style="#f28a1e", expand=False))

    # ---- agent turns ------------------------------------------------------------
    async def run_agent(self, text: str, mode: str | None = None):
        if mode:
            self.app.set_mode(mode)
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(self.app.run(text))
        self._running = task
        self.task_status = "running"
        self._install_sigint()
        self.console.print("[dim]⋯ working (Ctrl+C to cancel)[/]")
        result = None
        try:
            result = await task
        except asyncio.CancelledError:
            self.renderer.stop()
            self.console.print("[yellow]✋ cancelled — running commands were stopped; the session is saved[/]")
        finally:
            self._running = None
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
            self.renderer.stop()
            self.task_status = "idle" if result is None or result.stop_reason == "completed" else result.stop_reason
            self.renderer.categories = {t.name: t.category for t in self.app.registry.list()}
        return result

    async def shell(self, cmd: str) -> None:
        """`!cmd`: run a command yourself, attached to the terminal (interactive programs work)."""
        if not cmd.strip():
            return
        rc = await asyncio.to_thread(subprocess.call, cmd, shell=True, cwd=self.app.project_root)
        self.console.print(f"[dim]exit code {rc}[/]")
        if self.app.agent:
            self.app.agent.history.append(Message("user", f"[the user ran a shell command themselves: `{cmd}` "
                                                          f"(exit code {rc}); its output was not captured]",
                                                  meta={"synthetic": True}))

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")
        def _(event):
            nxt = MODES[(MODES.index(self.app.mode) + 1) % len(MODES)]
            self.app.set_mode(nxt)
            self._refresh_toolbar()
            event.app.invalidate()

        return kb

    async def main(self) -> int:
        with self.console.status("starting Naga Code (checking provider and model)…"):
            await self.app.start()
        load_project_commands(self.app.project_root)
        for w in self.app.config.warnings + REGISTRY.warnings:
            self.warn(w)
        if self.initial_mode:
            self.app.set_mode(self.initial_mode)
        if self.resume_id is not None:
            try:
                s = self.app.resume(self.resume_id or None)
                self.success(f"resumed session {s.id}: {s.title}")
            except (KeyError, ValueError) as e:
                self.warn(str(e))
        self.renderer.categories = {t.name: t.category for t in self.app.registry.list()}
        self.banner()
        if not self.app.provider_status[0]:
            self.warn("model provider is not reachable — run /doctor. Slash commands still work.")
        hist = data_home() / "history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        self._load_ui_prefs()
        from prompt_toolkit.enums import EditingMode
        vim = bool(self.app.config.get("ui.vim", False))
        session: PromptSession = PromptSession(history=FileHistory(str(hist)), multiline=True,
                                               editing_mode=EditingMode.VI if vim else EditingMode.EMACS,
                                               key_bindings=self._bindings(), bottom_toolbar=self.toolbar,
                                               completer=SlashCompleter(self.app), complete_while_typing=True,
                                               enable_history_search=False)  # True would disable complete_while_typing
        self.prompt_session = session
        try:
            while True:
                self._refresh_toolbar()
                try:
                    color = style.prompt_color()
                    tag = color if color.startswith("ansi") else "style"
                    attr = "" if tag != "style" else f' fg="{color}"'
                    default, self.prefill = self.prefill, ""
                    text = await session.prompt_async(HTML(f"<{tag}{attr}><b>❯ </b></{tag}>"),
                                                      prompt_continuation="  ", default=default)
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/") and "\n" not in text.split(" ")[0]:
                    if not await REGISTRY.dispatch(self, self.app, text):
                        break
                    continue
                if text.startswith("!"):
                    await self.shell(text[1:])
                    continue
                result = await self.run_agent(text)
                if self.app.goal:
                    from ..commands_extra import after_turn
                    await after_turn(self, self.app, result)
        finally:
            await self.app.shutdown()
            self.console.print(f"[dim]session saved: {self.app.session.id} (resume with `naga --resume "
                               f"{self.app.session.id}`)[/]")
        return 0
