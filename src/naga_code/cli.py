"""`naga` command-line entry point."""

from __future__ import annotations

import argparse
import os
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .config import DEFAULTS, dump_toml, load_config, user_config_path

console = Console(highlight=False)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="naga", description="Naga Code — a local-first terminal coding agent.")
    p.add_argument("--version", action="version", version=f"naga-code {__version__}")
    p.add_argument("--cwd", type=Path, default=None, help="project directory (default: current directory)")
    p.add_argument("--profile", help="model profile (coding, fast, reasoning, vision, subagent, ...)")
    p.add_argument("--model", help="override the profile's model name")
    p.add_argument("--mode", choices=["ask", "plan", "agent"], help="initial agent mode")
    p.add_argument("--permission-mode", choices=["default", "accept-edits", "read-only"],
                   help="permission mode for this run")
    p.add_argument("--resume", nargs="?", const="", default=None, metavar="SESSION",
                   help="resume a session (latest if no id)")
    p.add_argument("--compact", action="store_true", default=None, help="compact UI for small terminals")
    p.add_argument("-p", "--print", dest="prompt", metavar="PROMPT",
                   help="non-interactive: run one task, print the result, exit")
    p.add_argument("--allow-edits", action="store_true", help="(-p) auto-approve workspace file edits")
    p.add_argument("--allow-commands", action="append", default=[], metavar="PREFIX",
                   help="(-p) auto-approve simple commands with this prefix (repeatable)")
    p.add_argument("--json", action="store_true", help="(-p) print a JSON result record")
    p.add_argument("--stream-json", action="store_true",
                   help="(-p) print one JSON object per event (text, tool calls, results, final) as they happen")
    p.add_argument("--phone-approvals", action="store_true",
                   help="(-p) ask on your phone (ntfy, see /notify) instead of denying actions that need approval")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="diagnose environment, providers and models")
    sub.add_parser("models", help="list installed models and configured profiles")
    cfg = sub.add_parser("config", help="show configuration")
    cfg.add_argument("action", nargs="?", choices=["show", "path", "init"], default="show")
    mcp = sub.add_parser("mcp", help="MCP servers")
    mcp.add_argument("action", nargs="?", choices=["list"], default="list")
    sk = sub.add_parser("skills", help="skills")
    sk.add_argument("action", nargs="?", choices=["list"], default="list")
    ss = sub.add_parser("sessions", help="list saved sessions for the project")
    ss.add_argument("action", nargs="?", choices=["list"], default="list")
    return p


def _overrides(args: argparse.Namespace) -> dict:
    o: dict = {}
    if args.permission_mode:
        o.setdefault("permissions", {})["mode"] = args.permission_mode
    if args.allow_edits:
        o.setdefault("permissions", {})["mode"] = "accept-edits"
    if args.allow_commands:
        base = load_config().get("permissions.allow_commands", [])
        o.setdefault("permissions", {})["allow_commands"] = list(base) + args.allow_commands
    return o


async def _headless(args: argparse.Namespace, cwd: Path) -> int:
    from .app import NagaApp
    from .ui.renderer import EventRenderer

    err = Console(stderr=True, highlight=False)
    renderer = EventRenderer(err, compact=True)
    from .api import event_to_json
    stream = (lambda e: print(json.dumps(event_to_json(e)), flush=True)) if args.stream_json else None
    app = NagaApp(cwd, overrides=_overrides(args), profile=args.profile, model=args.model,
                  on_event=stream or (None if args.json else renderer.handle),
                  notify=lambda k, m: err.print(f"[dim]{k}: {m}[/]"))
    # No approval handler: anything that needs approval is denied and reported (never silently allowed),
    # unless --phone-approvals is given and notifications are configured (/notify setup).
    from .remote_approval import Notifier, PhoneApprover
    notifier = Notifier.from_config(app.config)
    if args.phone_approvals:
        if notifier is None:
            err.print("[yellow]--phone-approvals ignored: notifications are not set up (run /notify setup)[/]")
        else:
            app.permissions.handler = PhoneApprover(notifier, label=f"Naga ({cwd.name})")
    await app.start()
    if not app.provider_status[0]:
        err.print(f"[red]provider unavailable: {app.provider_status[1]}[/]")
        await app.shutdown()
        return 2
    if args.mode:
        app.set_mode(args.mode)
    if args.resume is not None:
        app.resume(args.resume or None)
    try:
        result = await app.run(args.prompt)
    finally:
        await app.shutdown()
    if notifier is not None and os.environ.get("NAGA_JOB_ID"):
        try:   # tell the phone a background job is done
            await notifier.publish(f"Naga job {os.environ['NAGA_JOB_ID']} {result.stop_reason}",
                                   (result.text or "")[:600] + (f"\nchanged: {', '.join(result.files_changed[:8])}"
                                                               if result.files_changed else ""),
                                   tags=["white_check_mark" if result.stop_reason == "completed" else "x"])
        except Exception:  # noqa: BLE001 - a failed notification must not fail the job
            pass
        finally:
            await notifier.aclose()
    if args.stream_json:
        pass   # the "final" event already carried the result
    elif args.json:
        print(json.dumps({"text": result.text, "stop_reason": result.stop_reason, "iterations": result.iterations,
                          "tool_calls": result.tool_calls, "files_changed": result.files_changed,
                          "validations": result.validations, "session": app.session.id,
                          "model_calls": app.agent.call_log if app.agent else []}, indent=2))
    else:
        print(result.text)
    return 0 if result.stop_reason == "completed" else 1


async def _doctor(cwd: Path) -> int:
    from .app import NagaApp
    from .diagnostics import run_doctor
    app = NagaApp(cwd)
    icons = {"ok": "[green]✔[/]", "warn": "[yellow]![/]", "fail": "[red]✘[/]", "info": "[blue]i[/]"}
    checks = await run_doctor(app)
    for c in checks:
        console.print(f"{icons[c.status]} [bold]{c.name}[/] {c.detail}")
    await app.shutdown()
    return 1 if any(c.status == "fail" for c in checks if c.name.startswith(("provider:", "profile:coding", "storage"))) else 0


async def _models(cwd: Path) -> int:
    from .app import NagaApp
    from .providers import ProviderError
    app = NagaApp(cwd)
    for name, spec in app.config.section("providers").items():
        if not spec.get("enabled", True):
            continue
        try:
            models = await app.get_provider(name).list_models()
        except ProviderError as e:
            console.print(f"[red]{name}: {e}[/]")
            continue
        console.print(f"[bold]{name}[/] ({spec['kind']} at {spec['base_url']})")
        for m in models:
            console.print(f"  {m.name:<32} {(m.size_bytes or 0) / 1e9:6.1f} GB  ctx {str(m.context_length or '?'):>7}  "
                          f"{', '.join(m.capabilities)}")
    console.print("\n[bold]profiles[/]")
    for n, p in app.config.section("profiles").items():
        console.print(f"  {n:<10} {p['provider']}:{p['model']} (num_ctx {p.get('num_ctx')}, tool_mode {p.get('tool_mode')})")
    console.print("\n[dim]Models are never downloaded automatically. To add one: `ollama pull <name>` "
                  "(check its size first), then set it in a profile with `naga config` / ~/.config/naga/config.toml.[/]")
    await app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cwd = (args.cwd or Path.cwd()).expanduser().resolve()
    if not cwd.is_dir():
        console.print(f"[red]--cwd {cwd} is not a directory[/]")
        return 2
    if args.cmd == "doctor":
        return asyncio.run(_doctor(cwd))
    if args.cmd == "models":
        return asyncio.run(_models(cwd))
    if args.cmd == "config":
        if args.action == "path":
            console.print(str(user_config_path()))
        elif args.action == "init":
            path = user_config_path()
            if path.exists():
                console.print(f"[yellow]{path} already exists; not overwriting[/]")
                return 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_toml({k: DEFAULTS[k] for k in ("general", "providers", "profiles", "permissions")}))
            console.print(f"wrote {path}")
        else:
            from .workspace import find_project_root
            cfg = load_config(find_project_root(cwd))
            console.print(dump_toml(cfg.data))
            console.print(f"# sources: {', '.join(cfg.sources)}")
            for w in cfg.warnings:
                console.print(f"# warning: {w}")
        return 0
    if args.cmd == "skills":
        from .skills import SkillLibrary
        from .workspace import find_project_root
        lib = SkillLibrary(find_project_root(cwd))
        for s in lib.skills.values():
            console.print(f"[bold]{s.name}[/] [dim]({s.source})[/] {s.description}")
        for prob in lib.problems:
            console.print(f"[yellow]invalid: {prob.path}: {prob.error}[/]")
        return 0
    if args.cmd == "mcp":
        from .mcp.manager import MCPManager
        from .tools.registry import ToolRegistry
        from .workspace import find_project_root
        mgr = MCPManager(find_project_root(cwd), ToolRegistry())
        if not mgr.servers:
            console.print("no MCP servers configured (see examples/mcp.example.json)")
        for n, st in mgr.servers.items():
            console.print(f"[bold]{n}[/] [{st.config.source}] {st.config.endpoint}  permission={st.config.permission} "
                          f"{'trusted' if mgr.trusted(st.config) else 'UNTRUSTED (approve with /mcp trust)'}"
                          f"{'' if st.config.enabled else ' (disabled)'}")
        for e in mgr.errors:
            console.print(f"[yellow]{e}[/]")
        return 0
    if args.cmd == "sessions":
        from .memory.sessions import SessionStore
        from .workspace import find_project_root
        for r in SessionStore(find_project_root(cwd)).list():
            console.print(f"{r['id']}  {r['messages']:>4} msgs  {r['model']:<22} {r['title'][:60]}")
        return 0
    if args.prompt:
        return asyncio.run(_headless(args, cwd))
    if not sys.stdin.isatty():
        console.print("[red]interactive mode needs a terminal; use `naga -p \"task\"` for scripts[/]")
        return 2
    from .ui.terminal_app import TerminalApp
    ui = TerminalApp(cwd, overrides=_overrides(args), profile=args.profile, model=args.model, compact=args.compact,
                     resume=args.resume, mode=args.mode)
    return asyncio.run(ui.main())


if __name__ == "__main__":
    sys.exit(main())
