"""Slash-command registry and built-in commands.

Commands have explicit semantics; only a few (e.g. /init, /review, /skill, /plan) start an agent turn,
and they say so in their help text. Project commands from ``.naga/commands/*.md`` are prompt templates
(``$ARGUMENTS`` is replaced); they can never silently replace a built-in command.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import checkpoints
from . import git as G
from .config import data_home, dump_toml, set_user_value, user_config_path
from .diagnostics import run_doctor
from .providers import ProviderError, ToolCall
from .tools.testing import detect_project

if TYPE_CHECKING:
    from rich.console import Console

    from .agent.runtime import TurnResult
    from .app import NagaApp
    from .tools.registry import ToolResult


class UI(Protocol):
    console: "Console"
    verbose: bool
    last_result: "ToolResult | None"

    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def success(self, msg: str) -> None: ...
    async def confirm(self, question: str, default: bool = False) -> bool: ...
    async def choose(self, question: str, options: list[tuple[str, str]], default: int = 0) -> str | None: ...
    async def run_agent(self, text: str, mode: str | None = None) -> "TurnResult | None": ...
    def show_diff(self, diff: str) -> None: ...


Handler = Callable[["UI", "NagaApp", str], Awaitable[None]]


@dataclass
class Command:
    name: str
    help: str
    handler: Handler
    usage: str = ""
    aliases: list[str] = field(default_factory=list)
    group: str = "session"
    protected: bool = True
    starts_agent: bool = False
    hidden: bool = False        # left out of completion and the main /help list (e.g. cloud-only commands)


class CommandRegistry:
    def __init__(self) -> None:
        self.commands: dict[str, Command] = {}
        self.aliases: dict[str, str] = {}
        self.warnings: list[str] = []

    def add(self, cmd: Command) -> None:
        if cmd.name in self.commands or cmd.name in self.aliases:
            existing = self.commands.get(self.aliases.get(cmd.name, cmd.name))
            if existing and existing.protected:
                renamed = f"project:{cmd.name}"
                self.warnings.append(f"project command /{cmd.name} conflicts with a built-in; available as /{renamed}")
                cmd.name = renamed
        self.commands[cmd.name] = cmd
        for a in cmd.aliases:
            self.aliases[a] = cmd.name

    def resolve(self, name: str) -> Command | None:
        return self.commands.get(self.aliases.get(name, name))

    def names(self, include_hidden: bool = True) -> list[str]:
        if include_hidden:
            return sorted(list(self.commands) + list(self.aliases))
        return sorted([n for n, c in self.commands.items() if not c.hidden] +
                      [a for a, n in self.aliases.items() if not self.commands[n].hidden])

    async def dispatch(self, ui: UI, app: "NagaApp", line: str) -> bool:
        """Run a slash command line. Returns False if the session should exit."""
        name, _, args = line[1:].partition(" ")
        cmd = self.resolve(name.strip())
        if cmd is None:
            close = [n for n in self.names(include_hidden=False) if n.startswith(name[:2])][:5]
            ui.error(f"unknown command /{name}" + (f" — did you mean {', '.join('/' + c for c in close)}?" if close else
                                                    " — /help lists commands"))
            return True
        if cmd.name == "exit":
            return False
        try:
            await cmd.handler(ui, app, args.strip())
        except (ValueError, KeyError, ProviderError, OSError) as e:
            ui.error(f"/{cmd.name}: {e}")
        return True


REGISTRY = CommandRegistry()


def command(name: str, help: str, usage: str = "", aliases: list[str] | None = None, group: str = "session",
            starts_agent: bool = False, hidden: bool = False):
    def deco(fn: Handler) -> Handler:
        REGISTRY.add(Command(name, help, fn, usage, aliases or [], group, True, starts_agent, hidden))
        return fn
    return deco


def _table(*cols: str, title: str | None = None) -> Table:
    t = Table(title=title, show_header=True, header_style="bold", box=None, pad_edge=False)
    for c in cols:
        t.add_column(c, overflow="fold")
    return t


def _ago(ts: float) -> str:
    d = time.time() - ts
    for unit, s in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= s:
            return f"{int(d // s)}{unit} ago"
    return "just now"


async def _tool(ui: UI, app: "NagaApp", name: str, args: dict[str, Any]) -> "ToolResult":
    """Run a tool on the user's behalf through the normal validation/permission path."""
    res = await app.registry.execute(ToolCall(name=name, arguments=args), app.ctx)
    ui.last_result = res
    return res


# ------------------------------------------------------------------ session
@command("help", "List commands, or show help for one command.", "/help [command]")
async def _help(ui: UI, app: "NagaApp", args: str) -> None:
    if args:
        c = REGISTRY.resolve(args.lstrip("/"))
        if not c:
            raise ValueError(f"no command /{args}")
        ui.console.print(f"[bold]/{c.name}[/] {c.usage}\n{c.help}" + (f"\naliases: {', '.join(c.aliases)}" if c.aliases else ""))
        return
    groups: dict[str, list[Command]] = {}
    for c in REGISTRY.commands.values():
        groups.setdefault("not available" if c.hidden else c.group, []).append(c)
    for g in ("session", "repository", "skills", "git", "plugin", "project", "not available"):
        if g not in groups:
            continue
        t = _table("command", "description", title=g.capitalize() + (" (cloud/account features)" if g == "not available" else ""))
        for c in sorted(groups[g], key=lambda c: c.name):
            t.add_row(f"/{c.name}" + (f" ({', '.join('/' + a for a in c.aliases)})" if c.aliases else ""),
                      c.help + (" [dim](runs the agent)[/]" if c.starts_agent else ""))
        ui.console.print(t)
    ui.console.print("[dim]Keys: Enter send · Esc,Enter or Ctrl+J newline · Shift+Tab cycle mode · Ctrl+C cancel task · "
                     "Ctrl+D exit · !cmd run a shell command yourself[/]")


@command("exit", "Exit Naga Code (session is saved).", aliases=["quit"])
async def _exit(ui: UI, app: "NagaApp", args: str) -> None:  # handled in dispatch
    return None


@command("clear", "Start a new conversation with empty context; the old one stays resumable. A name labels it "
                  "in /resume. (Ctrl+L clears the screen.)", "/clear [name]", aliases=["reset", "new"])
async def _clear(ui: UI, app: "NagaApp", args: str) -> None:
    if args:
        app.session.title = args
    previous = app.session.id if app.agent and app.agent.history else None
    app.reset()
    ui.console.clear()
    ui.success(f"new conversation {app.session.id}" + (f" (previous: {previous}, /resume {previous})" if previous else ""))


@command("status", "Show session, model, provider, mode, context and task status.")
async def _status(ui: UI, app: "NagaApp", args: str) -> None:
    s = app.status()
    t = _table("", "")
    ok, detail = s["provider"]
    rows = [("project", s["project"]), ("session", f"{s['session']} ({s['messages']} messages)"),
            ("model", f"{s['model']} [profile {s['profile']}, {s['tool_mode']} tools]"),
            ("provider", f"{'[green]connected[/]' if ok else '[red]unavailable[/]'} — {detail}"),
            ("mode", f"{s['mode']} · permissions: {s['permission_mode']}"),
            ("context", f"~{s['context_used']:,} / {s['context_total']:,} tokens "
                        f"({100 * s['context_used'] / max(s['context_total'], 1):.0f}%)"),
            ("git", s["git"]), ("agent changes", str(s["changes"])), ("sandbox", s["sandbox"])]
    if app.agent:
        u = app.agent.total_usage
        rows.append(("tokens used", f"prompt {u.prompt_tokens or 0:,} · completion {u.completion_tokens or 0:,} · "
                                    f"model time {u.duration_s or 0:.1f}s"))
    plan = app.session.task.plan
    if plan.steps:
        done = sum(s.status == "completed" for s in plan.steps)
        rows.append(("task", f"{plan.objective[:60]} — {done}/{len(plan.steps)} steps"))
    for k, v in rows:
        t.add_row(f"[bold]{k}[/]", v)
    ui.console.print(t)


@command("mode", "Show or set the agent mode: ask (read-only), plan (plan first), agent (execute).", "/mode [ask|plan|agent]")
async def _mode(ui: UI, app: "NagaApp", args: str) -> None:
    if args:
        app.set_mode(args)
    ui.info(f"mode: {app.mode}")


@command("model", "Pick a model with the arrow keys, or switch directly: /model <profile|model>. "
                  "/model list shows the tables.", "/model [list|profile|model]")
async def _model(ui: UI, app: "NagaApp", args: str) -> None:
    if args and args != "list":
        if args in app.config.section("profiles"):
            name = await app.configure_model(profile=args)
        else:
            name = await app.configure_model(model=args)
        ui.success(f"using {name} (profile {app.profile_name}, ctx {app.context_length:,}, {app.tool_mode} tool calls)")
        return
    try:
        base_profile = app.profile_name
        if app.config.profile(base_profile).get("custom"):   # list the regular models, not the custom server's
            base_profile = next((n for n, p in app.config.section("profiles").items() if not p.get("custom")), base_profile)
        prov = app.get_provider(app.config.profile(base_profile)["provider"])
        models = await prov.list_models()
    except ProviderError as e:
        ui.warn(str(e))
        models = []
    if not args and (models or any(p.get("custom") for p in app.config.section("profiles").values())) \
            and hasattr(ui, "choose"):
        chat = [m for m in models if m.capabilities != ["embedding"]]
        custom = {n: p for n, p in app.config.section("profiles").items() if p.get("custom")}
        width = max([len(m.name) for m in chat] + [len(n) for n in custom] + [1])
        opts = [(m.name, f"{m.name.ljust(width)}  {(m.size_bytes or 0) / 1e9:5.1f} GB  "
                         f"{', '.join(c for c in m.capabilities if c != 'completion') or '-'}"
                         + ("  (current)" if m.name == app.model_name else "")) for m in chat]
        for n, p in custom.items():
            url = app.config.section("providers").get(p["provider"], {}).get("base_url", "")
            opts.append((f"profile:{n}", f"{n.ljust(width)}  custom · {url}" +
                         ("  (current)" if n == app.profile_name else "")))
        current = next((i for i, (v, _) in enumerate(opts) if v == f"profile:{app.profile_name}"),
                       next((i for i, m in enumerate(chat) if m.name == app.model_name), 0))
        picked = await ui.choose(f"Select a model (profile {app.profile_name})", opts, default=current)
        if picked is None:
            ui.info("model unchanged")
            return
        if picked.startswith("profile:"):
            name = await app.configure_model(profile=picked.split(":", 1)[1])
            ui.success(f"using {name} (custom model {app.profile_name}, {app.tool_mode} tool calls)")
            if await ui.choose(f"Use '{app.profile_name}' when Naga starts?",
                               [("session", "No, just this session"), ("default", "Yes, make it the default")]) == "default":
                set_user_value("general.default_profile", f'"{app.profile_name}"')
                ui.success("saved as the default")
            return
        if picked != app.model_name or base_profile != app.profile_name:
            if base_profile != app.profile_name:
                await app.configure_model(profile=base_profile)
            name = await app.configure_model(model=picked)
            ui.success(f"using {name} (profile {app.profile_name}, ctx {app.context_length:,}, "
                       f"{app.tool_mode} tool calls)")
        scope = await ui.choose(f"Keep {picked} for future sessions too?",
                                [("session", "No, just this session"),
                                 ("default", f"Yes, make it the default for the '{app.profile_name}' profile")])
        if scope == "default":
            path = set_user_value(f"profiles.{app.profile_name}.model", f'"{picked}"')
            ui.success(f"saved as the default in {path}")
        return
    t = _table("profile", "provider", "model", "num_ctx", title="Profiles")
    for n, p in app.config.section("profiles").items():
        mark = " [green]●[/]" if n == app.profile_name else ""
        t.add_row(n + mark, p.get("provider", ""), p.get("model", ""), str(p.get("num_ctx", "")))
    ui.console.print(t)
    if models:
        t2 = _table("installed model", "size", "context", "capabilities", title=f"Models on {prov.name}")
        for m in models:
            t2.add_row(m.name + (" [green]●[/]" if m.name == app.model_name else ""),
                       f"{(m.size_bytes or 0) / 1e9:.1f} GB" if m.size_bytes else "?", str(m.context_length or "?"),
                       ", ".join(m.capabilities))
        ui.console.print(t2)


@command("provider", "List configured providers and test their connections.", "/provider [test]")
async def _provider(ui: UI, app: "NagaApp", args: str) -> None:
    t = _table("provider", "kind", "endpoint", "status")
    for name, spec in app.config.section("providers").items():
        if not spec.get("enabled", True):
            t.add_row(name, spec.get("kind", ""), spec.get("base_url", ""), "[dim]disabled[/]")
            continue
        ok, detail = await app.get_provider(name).health()
        t.add_row(name, spec.get("kind", ""), spec.get("base_url", ""), ("[green]ok[/] " if ok else "[red]down[/] ") + detail)
    ui.console.print(t)
    ui.console.print("[dim]Inference stays local: providers point at local endpoints unless you configure otherwise.[/]")


@command("context", "Show context usage, loaded instruction files and compaction state ('all' adds a per-message "
                    "breakdown).", "/context [all]")
async def _context(ui: UI, app: "NagaApp", args: str) -> None:
    used, total = app.context_usage()
    pct = 100 * used / max(total, 1)
    bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
    ui.console.print(f"context: [{('red' if pct > 75 else 'yellow' if pct > 50 else 'green')}]{bar}[/] "
                     f"~{used:,}/{total:,} tokens ({pct:.0f}%)")
    if app.agent:
        cm = app.agent.context
        ui.console.print(f"messages: {len(app.agent.history)} · compactions: {cm.compactions} · "
                         f"chars/token: {cm.chars_per_token:.2f}" +
                         (f" · last real prompt: {cm.last_prompt_tokens:,} tokens" if cm.last_prompt_tokens else ""))
    if app.instruction_files:
        ui.console.print("instruction files loaded:")
        for f in app.instruction_files:
            ui.console.print(f"  - {f.path} [{f.scope}]{' (truncated)' if f.truncated else ''}")
    else:
        ui.console.print("instruction files: none (create one with /init)")
    mem = app.memory.all()
    ui.console.print(f"persistent memory entries: {len(mem)} · skills indexed: {len(app.skills.skills)}")
    if args == "all" and app.agent:
        cpt = app.agent.context.chars_per_token or 4
        sizes: dict[str, int] = {"system prompt": len(app.system_prompt())}
        for m in app.agent.history:
            key = f"tool: {m.name}" if m.role == "tool" else m.role + (" (runtime)" if m.meta.get("synthetic") else "")
            sizes[key] = sizes.get(key, 0) + len(m.content) + sum(len(json.dumps(c.arguments)) for c in m.tool_calls)
        sizes["tool schemas"] = len(json.dumps(app.registry.schemas()))
        t = _table("part", "~tokens")
        for k, v in sorted(sizes.items(), key=lambda kv: -kv[1]):
            t.add_row(k, f"{int(v / cpt):,}")
        ui.console.print(t)


@command("compact", "Summarize older conversation to free context; optional focus instructions for the summary.",
         "/compact [instructions]")
async def _compact(ui: UI, app: "NagaApp", args: str) -> None:
    before, _ = app.context_usage()
    note = await app.compact(args or None)
    after, _ = app.context_usage()
    ui.success(f"{note}: ~{before:,} -> ~{after:,} tokens")


@command("sessions", "List saved sessions for this project.")
async def _sessions(ui: UI, app: "NagaApp", args: str) -> None:
    rows = app.sessions.list()
    if not rows:
        ui.info("no saved sessions yet")
        return
    t = _table("id", "updated", "msgs", "model", "title")
    for r in rows[:30]:
        t.add_row(r["id"] + (" [green]●[/]" if r["id"] == app.session.id else ""), _ago(r["updated"]),
                  str(r["messages"]), r["model"], r["title"][:60])
    ui.console.print(t)


@command("resume", "Resume a saved session (latest if no id; id prefixes work).", "/resume [session-id]",
         aliases=["continue"])
async def _resume(ui: UI, app: "NagaApp", args: str) -> None:
    if not args and app.agent and app.agent.history:
        await _sessions(ui, app, "")
        ui.info("use /resume <id> to switch sessions")
        return
    s = app.resume(args or None)
    ui.success(f"resumed {s.id}: {s.title} ({len(s.messages)} messages)")
    for m in [m for m in s.messages if m.role in {"user", "assistant"} and m.content and not m.meta.get("synthetic")][-4:]:
        ui.console.print(f"[bold]{m.role}:[/] {m.content[:300]}")
    if s.task.plan.steps:
        ui.console.print(Panel(s.task.plan.render(), title="task plan"))


@command("export", "Export the conversation and task report as Markdown.", "/export [path]")
async def _export(ui: UI, app: "NagaApp", args: str) -> None:
    app.save()
    path = Path(args).expanduser() if args else data_home() / "exports" / f"naga-{app.session.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    s, task = app.session, app.session.task
    out = [f"# Naga Code session {s.id}", f"- project: {s.project}", f"- model: {s.model}",
           f"- exported: {time.strftime('%Y-%m-%d %H:%M')}", "", "## Task report", "", "```", task.plan.render(), "```", ""]
    out.append("### Files changed by the agent")
    out += [f"- {c.summary()} (via {c.tool}, {c.agent})" for c in app.workspace.changes] or ["- none"]
    out.append("\n### Commands executed")
    out += [f"- `{c['command']}` -> exit {c['exit_code']}{' (timed out)' if c.get('timed_out') else ''}" for c in task.commands] or ["- none"]
    out.append("\n### Validations")
    out += [f"- {v['kind']}: `{v['command']}` **{v['verdict']}** {v.get('summary') or ''}" for v in task.validations] or ["- none"]
    out.append("\n### Approval decisions")
    out += [f"- {d['tool']}: {d['decision']} — `{d['operation'][:100]}`" for d in app.permissions.decisions] or ["- none"]
    out.append("\n## Conversation\n")
    for m in s.messages:
        if m.role == "tool":
            out.append(f"<details><summary>tool result: {m.name}</summary>\n\n```\n{m.content[:4000]}\n```\n</details>\n")
        elif m.role == "assistant":
            out.append(f"**assistant:**\n\n{m.content}\n")
            for c in m.tool_calls:
                out.append(f"> tool call `{c.name}` `{json.dumps(c.arguments)[:400]}`\n")
        else:
            out.append(f"**{m.role}{' (runtime)' if m.meta.get('synthetic') else ''}:**\n\n{m.content}\n")
    path.write_text("\n".join(out))
    ui.success(f"exported to {path}")


@command("config", "Show merged configuration, or set user-level values.",
         "/config [path | get KEY | set KEY VALUE | key=value ...]", aliases=["settings"])
async def _config(ui: UI, app: "NagaApp", args: str) -> None:
    if args and "=" in args.split()[0] and not args.startswith(("get ", "set ")):
        for pair in shlex.split(args):
            key, _, value = pair.partition("=")
            path = set_user_value(key.strip(), value.strip())
            ui.success(f"set {key.strip()} in {path}")
        ui.info("restart naga to apply config changes")
        return
    parts = args.split(maxsplit=2)
    if parts and parts[0] == "path":
        ui.info(f"user config: {user_config_path()}\nproject config: {app.project_root / '.naga' / 'config.toml'}")
    elif parts and parts[0] == "get" and len(parts) >= 2:
        ui.console.print(repr(app.config.get(parts[1])))
    elif parts and parts[0] == "set" and len(parts) == 3:
        path = set_user_value(parts[1], parts[2])
        ui.success(f"set {parts[1]} in {path}; restart naga to apply")
    else:
        ui.console.print(Syntax(dump_toml(app.config.data), "toml", theme="ansi_dark", word_wrap=True))
        ui.console.print(f"[dim]sources: {', '.join(app.config.sources)}[/]")
        for w in app.config.warnings:
            ui.warn(w)


@command("doctor", "Diagnose providers, models, GPU, tools, sandbox and configuration.", aliases=["checkup"])
async def _doctor(ui: UI, app: "NagaApp", args: str) -> None:
    icons = {"ok": "[green]✔[/]", "warn": "[yellow]![/]", "fail": "[red]✘[/]", "info": "[blue]i[/]"}
    t = _table("", "check", "detail")
    for c in await run_doctor(app):
        t.add_row(icons[c.status], c.name, c.detail)
    ui.console.print(t)


@command("permissions", "Show/manage permissions: mode, allow-list, session grants.",
         "/permissions [mode default|accept-edits|read-only] [allow PREFIX] [revoke ID]", aliases=["allowed-tools"])
async def _permissions(ui: UI, app: "NagaApp", args: str) -> None:
    pe = app.permissions
    parts = args.split(maxsplit=1)
    if parts and parts[0] == "mode" and len(parts) == 2:
        if parts[1] not in {"default", "accept-edits", "read-only"}:
            raise ValueError("mode must be default, accept-edits or read-only")
        pe.mode = parts[1]
        ui.success(f"permission mode: {pe.mode} (this session)")
        return
    if parts and parts[0] == "allow" and len(parts) == 2:
        pe.allow_commands.append(parts[1])
        ui.success(f"commands starting with '{parts[1]}' auto-approved for this session (simple commands only)")
        return
    if parts and parts[0] == "revoke" and len(parts) == 2:
        ok = pe.revoke(int(parts[1].lstrip("#")))
        (ui.success if ok else ui.error)(f"grant #{parts[1]} {'revoked' if ok else 'not found'}")
        return
    ui.console.print(f"[bold]agent mode[/]: {pe.agent_mode}   [bold]permission mode[/]: {pe.mode}")
    ui.console.print("[bold]levels[/]: read (auto) · write (workspace edits; ask unless accept-edits) · execute (ask) · "
                     "dangerous (always ask, never grantable) · blocked (refused)")
    ui.console.print("[bold]auto-approved commands[/]: " + ", ".join(pe.allow_commands))
    if pe.deny_commands:
        ui.console.print("[bold]denied commands[/]: " + ", ".join(pe.deny_commands))
    ui.console.print("[bold]session grants[/]: " + (", ".join(g.describe() for g in pe.grants) or "none"))
    if pe.decisions:
        t = _table("tool", "decision", "operation", title="recent decisions")
        for d in pe.decisions[-10:]:
            t.add_row(d["tool"], d["decision"], d["operation"][:80])
        ui.console.print(t)


@command("tools", "List tools with their category and permission level.")
async def _tools(ui: UI, app: "NagaApp", args: str) -> None:
    t = _table("tool", "category", "level", "description")
    for tool in app.registry.list():
        t.add_row(tool.name, tool.category, tool.level.name.lower() + (" +net" if tool.network else ""),
                  tool.description[:90])
    ui.console.print(t)


@command("verbose", "Toggle showing full tool output and model thinking.")
async def _verbose(ui: UI, app: "NagaApp", args: str) -> None:
    ui.verbose = not ui.verbose
    ui.info(f"verbose {'on' if ui.verbose else 'off'}")


@command("last", "Show the full output of the last tool result.")
async def _last(ui: UI, app: "NagaApp", args: str) -> None:
    if not ui.last_result:
        ui.info("no tool result yet")
        return
    ui.console.print(ui.last_result.output)
    if ui.last_result.diff:
        ui.show_diff(ui.last_result.diff)


# --------------------------------------------------------------- repository
INIT_PROMPT = """Create or update the project instruction file NAGA.md at the repository root.
Inspect the repository first (repo_summary, manifests, README, test/lint config, CI workflows). NAGA.md must be
concise (under ~80 lines) and contain only facts you verified: project overview, architecture/layout, exact
build/test/lint commands, coding conventions you observed, and things to avoid. If NAGA.md exists, read it and
improve it while preserving the user's content. Then show what you wrote."""


@command("init", "Create/update NAGA.md project instructions (runs the agent; --template writes a draft offline).",
         "/init [--template]", group="repository", starts_agent=True)
async def _init(ui: UI, app: "NagaApp", args: str) -> None:
    target = app.project_root / "NAGA.md"
    if args.strip() == "--template":
        if target.exists() and not await ui.confirm("NAGA.md exists. Overwrite with a template?"):
            return
        pp = detect_project(app.project_root)
        target.write_text(f"# {app.project_root.name}\n\n## Overview\n(describe the project)\n\n## Commands\n"
                          f"- test: {', '.join(pp.test) or '(fill in)'}\n- lint: {', '.join(pp.lint) or '(fill in)'}\n"
                          f"- build: {', '.join(pp.build) or '(fill in)'}\n\n## Conventions\n- \n\n## Do not\n- \n")
        ui.success(f"wrote {target}")
    else:
        await ui.run_agent(INIT_PROMPT, mode="agent")
    from .agent import instructions as instr
    app.instruction_files = instr.discover(app.project_root, app.cwd)


@command("plan", "Show the plan, or plan a task in plan mode and approve it before implementing.",
         "/plan [task]", group="repository", starts_agent=True)
async def _plan(ui: UI, app: "NagaApp", args: str) -> None:
    plan = app.session.task.plan
    if not args:
        ui.console.print(Panel(plan.render(), title="task plan"))
        return
    previous = app.mode
    result = await ui.run_agent(args, mode="plan")
    if result is None or result.stop_reason != "completed":
        return
    plan = app.session.task.plan
    if not plan.steps:
        ui.warn("the model did not record a structured plan (update_plan); review its answer above")
    else:
        ui.console.print(Panel(plan.render(), title="proposed plan"))
    if await ui.confirm("Approve this plan and implement it now in agent mode?"):
        plan.approved = True
        await ui.run_agent("The plan is approved. Implement it step by step, updating step status as you go, then run "
                           "the validation commands and report real results.", mode="agent")
    else:
        app.set_mode(previous if previous != "agent" else "plan")
        ui.info("plan not approved; refine it by replying, or /mode agent to proceed differently")


@command("tasks", "Show task progress: plan steps, commands run, validations, notes.", group="repository")
async def _tasks(ui: UI, app: "NagaApp", args: str) -> None:
    task = app.session.task
    ui.console.print(Panel(task.plan.render(), title=f"task {task.id}"))
    if task.commands:
        t = _table("command", "kind", "exit", "duration")
        for c in task.commands[-15:]:
            t.add_row(c["command"][:80], c["kind"], "timeout" if c.get("timed_out") else str(c["exit_code"]),
                      f"{c['duration_s']}s")
        ui.console.print(t)
    for v in task.validations[-10:]:
        ui.console.print(f"{'[green]' if v['verdict'] == 'PASSED' else '[red]'}{v['verdict']}[/] {v['kind']}: "
                         f"{v['command']} {v.get('summary') or ''}")
    for n in task.notes[-10:]:
        ui.console.print(f"[dim]note:[/] {n}")
    if app.workspace.changes:
        ui.console.print("agent changes: " + ", ".join(c.summary() for c in app.workspace.changes[-20:]))


@command("files", "List project files (git-tracked + untracked, honoring ignores), optionally filtered.",
         "/files [glob]", group="repository")
async def _files(ui: UI, app: "NagaApp", args: str) -> None:
    import fnmatch
    files = app.workspace.git_files() or [app.workspace.rel(p) for p in app.workspace.walk()]
    if args:
        files = [f for f in files if fnmatch.fnmatch(f, args) or fnmatch.fnmatch(Path(f).name, args)]
    ui.console.print("\n".join(files[:500]) + (f"\n... {len(files) - 500} more" if len(files) > 500 else ""))
    ui.info(f"{len(files)} files")


@command("search", "Search source code (ripgrep regex).", "/search PATTERN [--glob GLOB]", group="repository")
async def _search(ui: UI, app: "NagaApp", args: str) -> None:
    if not args:
        raise ValueError("usage: /search PATTERN [--glob GLOB]")
    parts = shlex.split(args)
    a: dict[str, Any] = {"pattern": parts[0]}
    if "--glob" in parts:
        a["glob"] = parts[parts.index("--glob") + 1]
    res = await _tool(ui, app, "search_code", a)
    ui.console.print(res.output)


@command("diff", "Show the current git diff (or --staged).", "/diff [--staged] [path...]", group="git")
async def _diff(ui: UI, app: "NagaApp", args: str) -> None:
    parts = shlex.split(args)
    staged = "--staged" in parts
    paths = [p for p in parts if p != "--staged"]
    res = await _tool(ui, app, "git_diff", {"staged": staged, "paths": paths})
    if not res.ok:
        ui.error(res.output)
    elif res.output.strip() == "(no differences)":
        ui.info("no differences" + (" staged" if staged else " (try /diff --staged)"))
    else:
        ui.show_diff(res.output)


REVIEW_LEVELS = {"low": "Report only the few highest-confidence, highest-impact findings.",
                 "medium": "Report clear correctness bugs with evidence.",
                 "high": "Be thorough: correctness bugs plus likely edge-case failures.",
                 "xhigh": "Be exhaustive, including lower-confidence findings marked as such.",
                 "max": "Be exhaustive, including lower-confidence findings marked as such."}


@command("code-review", "Review the current diff (or a branch, path or PR number) for correctness bugs. Effort level "
                        "low..max; --fix applies the findings afterwards; 'ultra' runs /ultrareview.",
         "/code-review [low|medium|high|xhigh|max|ultra] [--fix] [--max-findings N] [target]", aliases=["review"],
         group="repository", starts_agent=True)
async def _review(ui: UI, app: "NagaApp", args: str) -> None:
    parts = shlex.split(args)
    if "ultra" in parts:
        parts.remove("ultra")
        return await REGISTRY.resolve("ultrareview").handler(ui, app, " ".join(parts))  # type: ignore[union-attr]
    level = next((p for p in parts if p in REVIEW_LEVELS), "medium")
    fix = "--fix" in parts
    limit = ""
    if "--max-findings" in parts:
        i = parts.index("--max-findings")
        limit = parts[i + 1] if i + 1 < len(parts) else ""
        del parts[i:i + 2]
    target = " ".join(p for p in parts if p not in REVIEW_LEVELS and p not in {"--fix", "--comment"})
    if target.isdigit():
        target = f"GitHub pull request #{target} (fetch it with `gh pr diff {target}`; that needs approval)"
    skill = app.skills.get("code-review")
    body = app.skills.render(skill, target) if skill else ""
    previous = app.mode
    result = await ui.run_agent(
        f"Review {target or 'the current uncommitted git diff (staged and unstaged)'} for correctness bugs. "
        f"{REVIEW_LEVELS[level]}" + (f" Report at most {limit} findings." if limit.isdigit() else "") +
        " Rank findings most severe first, each with file:line, the failure scenario and a suggested fix. "
        f"Do not modify files.\n\n{body}", mode="ask")
    app.set_mode(previous)
    if fix and result is not None and result.stop_reason == "completed":
        await ui.run_agent("Apply fixes for the review findings above that you are confident about, one at a time. "
                           "Validate with the relevant tests, then report which findings were fixed, skipped and why.",
                           mode="agent")


async def _validation(ui: UI, app: "NagaApp", args: str, kind: str) -> None:
    pp = detect_project(app.project_root)
    cmds = pp.test if kind == "test" else pp.lint
    a: dict[str, Any] = {}
    if args:
        a["command" if (" " in args or not cmds) else "target"] = args
    elif not cmds:
        ui.warn(f"no {kind} command detected for project types {pp.kinds or ['unknown']}; pass one: /{kind} <command>")
        return
    for n in pp.notes:
        ui.warn(n)
    res = await _tool(ui, app, "run_tests" if kind == "test" else "run_lint", a)
    ui.console.print(res.output if ui.verbose or not res.ok else res.output.splitlines()[0])
    (ui.success if res.ok else ui.error)(res.summary)


@command("test", "Detect and run tests for real (approval required). Arg: a target or a full command.",
         "/test [target | command]", group="repository")
async def _test(ui: UI, app: "NagaApp", args: str) -> None:
    await _validation(ui, app, args, "test")


@command("lint", "Run the configured linters/type checkers (approval required).", "/lint [command]", group="repository")
async def _lint(ui: UI, app: "NagaApp", args: str) -> None:
    await _validation(ui, app, args, "lint")


# ------------------------------------------------------------ skills/agents
@command("skills", "List available skills (built-in, user, project) and invalid skill files.", group="skills")
async def _skills(ui: UI, app: "NagaApp", args: str) -> None:
    app.skills.refresh()
    t = _table("skill", "source", "description")
    for s in app.skills.skills.values():
        t.add_row(s.name, s.source, s.description[:100])
    ui.console.print(t)
    for p in app.skills.problems:
        ui.warn(f"invalid skill {p.path}: {p.error}")


@command("skill", "Show a skill (/skill NAME) or invoke it with arguments (/skill NAME ARGS...).",
         "/skill NAME [arguments]", group="skills", starts_agent=True)
async def _skill(ui: UI, app: "NagaApp", args: str) -> None:
    name, _, rest = args.partition(" ")
    s = app.skills.get(name)
    if not s:
        raise ValueError(f"unknown skill '{name}' (see /skills)")
    if not rest:
        ui.console.print(Panel(Markdown(app.skills.render(s)), title=f"skill {s.name} ({s.source})"))
        ui.info(f"invoke with: /skill {s.name} <what to do>")
        return
    await ui.run_agent(f"Use the '{s.name}' skill for this request: {rest}\n\n{app.skills.render(s, rest)}")


@command("agents", "List subagents with their tools, permission ceiling and limits.", group="skills")
async def _agents(ui: UI, app: "NagaApp", args: str) -> None:
    t = _table("agent", "permission", "profile", "limits", "description")
    for d in app.subagents.defs.values():
        t.add_row(d.name, d.max_level.name.lower(), d.profile, f"{d.max_iterations} it / {d.timeout:.0f}s", d.description[:80])
    ui.console.print(t)


@command("delegate", "Run a subagent directly on a bounded task.", "/delegate AGENT TASK...", group="skills")
async def _delegate(ui: UI, app: "NagaApp", args: str) -> None:
    name, _, task = args.partition(" ")
    if not task:
        raise ValueError("usage: /delegate AGENT TASK")
    with ui.console.status(f"subagent {name} working..."):
        res = await app.subagents.run(name, task, app.ctx)
    ui.console.print(Panel(Markdown(res.render()), title=f"subagent {name}"))


@command("mcp", "Inspect/manage MCP servers.",
         "/mcp [list | connect N | disconnect N | reconnect N|all | enable N|all | disable N|all | trust N | tools N | resources N]",
         group="skills")
async def _mcp(ui: UI, app: "NagaApp", args: str) -> None:
    sub, _, name = args.partition(" ")
    mgr = app.mcp
    if sub in {"", "list"}:
        if not mgr.servers:
            ui.info("no MCP servers configured (~/.config/naga/mcp.json or .naga/mcp.json; see examples/mcp.example.json)")
        t = _table("server", "source", "status", "endpoint", "permission", "tools")
        for n, st in mgr.servers.items():
            trusted = mgr.trusted(st.config)
            t.add_row(n, st.config.source, st.status if trusted or st.status == "connected" else "untrusted",
                      st.config.endpoint[:60], st.config.permission, str(len(st.tool_names)))
        ui.console.print(t)
        for e in mgr.errors:
            ui.warn(e)
        return
    if sub in {"reconnect", "enable", "disable"} and name in {"", "all"}:
        for n in list(mgr.servers):
            await _mcp(ui, app, f"{sub} {n}")
        return
    st = mgr.servers.get(name)
    if st is None:
        raise ValueError(f"no MCP server '{name}'")
    if sub in {"reconnect", "enable"}:
        if sub == "reconnect":
            await mgr.disconnect(name)
        if not mgr.trusted(st.config):
            ui.warn(f"{name}: project server not trusted yet: /mcp trust {name}")
            return
        with ui.console.status(f"connecting {name}..."):
            st = await mgr.connect(name)
        (ui.success if st.status == "connected" else ui.error)(f"{name}: {st.status}" + (f" — {st.error}" if st.error else ""))
        return
    if sub == "disable":
        await mgr.disconnect(name)
        ui.success(f"{name}: disconnected for this session")
        return
    if sub == "trust":
        ui.console.print(Panel(f"{st.config.endpoint}\n\n{st.config.access_note()}", title=f"project MCP server {name}"))
        if await ui.confirm("Trust this exact server definition for this project?"):
            mgr.trust(name)
            ui.success("trusted; connect with /mcp connect " + name)
    elif sub == "connect":
        ui.console.print(Panel(f"{'URL' if st.config.url else 'command'}: {st.config.endpoint}\n{st.config.access_note()}\n"
                               f"tool calls will require: {st.config.permission} approval", title=f"connect {name}"))
        if not mgr.trusted(st.config):
            ui.warn(f"project server not trusted yet: /mcp trust {name}")
            return
        if not await ui.confirm("Connect?", default=True):
            return
        with ui.console.status(f"connecting {name}..."):
            st = await mgr.connect(name)
        if st.status == "connected":
            ui.success(f"connected to {st.client.server_info.get('name', name)} "  # type: ignore[union-attr]
                       f"(protocol {st.client.protocol_version}); {len(st.tool_names)} tools registered")  # type: ignore[union-attr]
        else:
            ui.error(f"{st.status}: {st.error}")
    elif sub == "disconnect":
        await mgr.disconnect(name)
        ui.success(f"disconnected {name}")
    elif sub == "tools":
        if not st.client:
            raise ValueError(f"{name} is not connected")
        for t in st.client.tools:
            ui.console.print(f"[bold]{t.name}[/]: {t.description}")
    elif sub == "resources":
        if not st.client:
            raise ValueError(f"{name} is not connected")
        for r in await st.client.list_resources():
            ui.console.print(f"{r.get('uri')}  {r.get('name', '')}")
        for p in await st.client.list_prompts():
            ui.console.print(f"prompt: {p.get('name')}  {p.get('description', '')}")
    else:
        raise ValueError("usage: /mcp [list | connect N | disconnect N | reconnect | enable | disable | trust N | tools N | resources N]")


@command("hooks", "Show configured lifecycle hooks; '/hooks trust' approves project hooks.", "/hooks [trust]", group="skills")
async def _hooks(ui: UI, app: "NagaApp", args: str) -> None:
    h = app.hooks
    if args == "trust":
        if not h.project_raw:
            ui.info("this project defines no hooks")
            return
        for spec in h.project:
            ui.console.print(f"  {spec.event}: {spec.command}" + (f" (match {spec.match})" if spec.match else ""))
        if await ui.confirm("These commands will run automatically on the listed events. Trust them?"):
            h.trust_project()
            ui.success("project hooks trusted (re-approval is required if they change)")
        return
    t = _table("source", "event", "command", "match", "active")
    for spec in h.user:
        t.add_row("user", spec.event, spec.command, spec.match or "", "yes")
    trusted = h.project_trusted()
    for spec in h.project:
        t.add_row("project", spec.event, spec.command, spec.match or "", "yes" if trusted else "no — /hooks trust")
    ui.console.print(t if (h.user or h.project) else "no hooks configured (see README: Hooks)")
    for e in h.errors:
        ui.warn(e)


@command("memory", "Inspect/manage persistent memory.",
         "/memory [list | add [--user] TEXT | rm SCOPE ID | edit SCOPE ID TEXT | export [PATH] | clear SCOPE]",
         group="skills")
async def _memory(ui: UI, app: "NagaApp", args: str) -> None:
    store = app.memory
    sub, _, rest = args.partition(" ")
    if sub in {"", "list"}:
        entries = store.all()
        for e in entries:
            ui.console.print(f"[{e.scope}] #{e.id} {e.text}")
        ui.info(f"{len(entries)} entries · files: " + ", ".join(str(p) for p in store.paths.values()))
    elif sub == "add":
        scope = "user" if rest.startswith("--user ") else "project"
        e = store.add(rest.removeprefix("--user ").strip(), scope)
        ui.success(f"saved #{e.id} ({e.scope})")
    elif sub == "rm":
        scope, _, eid = rest.partition(" ")
        (ui.success if store.remove(scope, int(eid)) else ui.error)(f"{scope} #{eid}")
    elif sub == "edit":
        scope, eid, text = rest.split(" ", 2)
        (ui.success if store.edit(scope, int(eid), text) else ui.error)(f"{scope} #{eid}")
    elif sub == "export":
        path = Path(rest).expanduser() if rest else data_home() / "exports" / "memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store.export(), indent=2))
        ui.success(f"exported to {path}")
    elif sub == "clear":
        if await ui.confirm(f"Delete all {rest} memory entries?"):
            store.clear(rest)
            ui.success(f"cleared {rest} memory")
    else:
        raise ValueError("unknown /memory subcommand")


# -------------------------------------------------------------------- git
@command("git", "Show git state and the git operations available.", group="git")
async def _git(ui: UI, app: "NagaApp", args: str) -> None:
    st = G.status(app.project_root)
    if not st.is_repo:
        ui.warn("not a git repository")
        return
    ui.console.print(f"[bold]{st.short()}[/]" + (f" (upstream {st.upstream})" if st.upstream else ""))
    for label, items in (("staged", st.staged), ("modified", st.modified), ("untracked", st.untracked)):
        if items:
            ui.console.print(f"{label}: " + ", ".join(items[:30]))
    res = await _tool(ui, app, "git_log", {"limit": 8})
    ui.console.print(res.output)
    ui.console.print("[dim]operations: /diff · /commit · /checkpoint · /undo · ask the agent to create a branch "
                     "(always requires approval). Destructive git commands are never run without explicit approval.[/]")


@command("undo", "Revert the agent's last file change if the file was not modified since.", group="git")
async def _undo(ui: UI, app: "NagaApp", args: str) -> None:
    if not app.workspace.changes:
        ui.info("no agent changes recorded in this session")
        return
    last = app.workspace.changes[-1]
    if not await ui.confirm(f"Revert: {last.summary()} (via {last.tool})?", default=True):
        return
    ok, msg = app.workspace.undo_last()
    if ok:
        ui.success(msg)
    else:
        first, _, patch = msg.partition("\n")
        ui.warn(first)
        if patch:
            ui.show_diff(patch)


@command("checkpoint", "Save a recoverable patch of all agent changes (or list checkpoints).", "/checkpoint [list | LABEL]",
         group="git")
async def _checkpoint(ui: UI, app: "NagaApp", args: str) -> None:
    if args == "list":
        for c in app.session.task.checkpoints:
            ui.console.print(f"#{c['n']} {c.get('label', '')} — {len(c['files'])} files — {c['patch']}")
        if not app.session.task.checkpoints:
            ui.info("no checkpoints")
        return
    path = checkpoints.create(app, args)
    ui.success(f"checkpoint saved: {path}\n  reverse patch: {path.with_suffix('').with_suffix('.reverse.patch')} "
               "(apply with /apply to roll back without touching unrelated changes)")


@command("apply", "Apply a reviewed unified-diff patch file (shows the diff and asks for approval).", "/apply PATCHFILE",
         group="git")
async def _apply(ui: UI, app: "NagaApp", args: str) -> None:
    if not args:
        raise ValueError("usage: /apply PATCHFILE")
    patch = Path(args).expanduser().read_text()
    ui.show_diff(patch)
    from .tools.patching import parse_patch
    for fp in parse_patch(patch):  # the agent must have "read" files for stale checks; the user is applying it
        for path in (fp.old_path, fp.new_path):
            if path:
                p = app.workspace.resolve(path, write=True)
                if p.is_file():
                    app.workspace.read_text(p)
    res = await _tool(ui, app, "apply_patch", {"patch": patch})
    (ui.success if res.ok else ui.error)(res.output)


@command("commit", "Prepare and create a git commit after approval.", "/commit [message]", group="git")
async def _commit(ui: UI, app: "NagaApp", args: str) -> None:
    st = G.status(app.project_root)
    if not st.is_repo:
        raise ValueError("not a git repository")
    if not st.staged:
        candidates = list(dict.fromkeys(app.workspace.changed_files() + st.modified))
        candidates = [c for c in candidates if c in st.modified or c in st.untracked]
        if not candidates:
            ui.info("nothing to commit")
            return
        ui.console.print("nothing staged. modified files:\n  " + "\n  ".join(candidates))
        if not await ui.confirm("Stage these files?"):
            return
        res = await _tool(ui, app, "git_stage", {"paths": candidates})
        if not res.ok:
            ui.error(res.output)
            return
    message = args
    if not message:
        diff = G.git(app.project_root, "diff", "--cached", "--stat").stdout + "\n" + \
            G.git(app.project_root, "diff", "--cached").stdout[:12000]
        assert app.agent
        with ui.console.status("drafting commit message..."):
            from .providers.base import Message
            message = (await app.agent.provider.complete(app.model_name, [
                Message("system", "Write a git commit message for this staged diff: an imperative subject line of at most "
                                  "72 characters, a blank line, then 1-4 short bullet lines explaining why. Output only the message."),
                Message("user", diff)], app.agent.options)).strip().strip("`")
        ui.console.print(Panel(message, title="proposed commit message"))
    res = await _tool(ui, app, "git_commit", {"message": message})
    (ui.success if res.ok else ui.error)(res.output)


def _template_handler(template: str) -> Handler:
    async def handler(ui: UI, app: "NagaApp", args: str) -> None:
        await ui.run_agent(template.replace("$ARGUMENTS", args))
    return handler


def load_project_commands(root: Path) -> None:
    """Register prompt-template commands from enabled plugins and the project's .naga/commands/."""
    from . import plugins
    for name in [n for n, c in REGISTRY.commands.items() if c.group in {"project", "plugin"}]:
        cmd = REGISTRY.commands.pop(name)
        for a in cmd.aliases:
            REGISTRY.aliases.pop(a, None)
    sources = [("plugin", f"[{p}]", d) for p, d in plugins.command_dirs()] + [("project", "[project]", root / ".naga" / "commands")]
    for group, tag, d in sources:
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            template = md.read_text(errors="replace")
            name = md.stem.lower().replace(" ", "-")
            first = next((ln.strip("# ").strip() for ln in template.splitlines() if ln.strip()), f"{group} command")
            REGISTRY.add(Command(name, f"{tag} {first[:80]}", _template_handler(template), "[arguments]", [], group,
                                 False, True))


from . import commands_extra  # noqa: E402,F401  (registers the remaining commands)
