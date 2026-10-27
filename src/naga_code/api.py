"""Use Naga from Python scripts.

    from naga_code.api import run
    result = run("Fix the failing test and run the tests", cwd="path/to/repo", allow_edits=True,
                 allow_commands=["python3 -m pytest"])
    print(result["text"], result["files_changed"], result["stop_reason"])

Like `naga -p`, there is no interactive approver: actions outside what you allow are denied and reported.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from .agent.runtime import AgentEvent


def event_to_json(e: AgentEvent) -> dict[str, Any]:
    d: dict[str, Any] = {"type": e.kind, "agent": e.agent}
    if e.text:
        d["text"] = e.text
    if e.call is not None:
        d["tool"] = {"name": e.call.name, "arguments": e.call.arguments}
    if e.result is not None:
        d["result"] = {"ok": e.result.ok, "summary": e.result.summary, "output": e.result.output[:4000]}
    if e.data:
        d["data"] = e.data
    return d


async def arun(prompt: str, cwd: str | Path = ".", *, model: str | None = None, profile: str | None = None,
               allow_edits: bool = False, allow_commands: list[str] | None = None, mode: str = "agent",
               on_event: Callable[[dict], None] | None = None) -> dict[str, Any]:
    from .app import NagaApp
    overrides: dict[str, Any] = {"permissions": {}}
    if allow_edits:
        overrides["permissions"]["mode"] = "accept-edits"
    if allow_commands:
        from .config import load_config
        overrides["permissions"]["allow_commands"] = load_config().get("permissions.allow_commands", []) + allow_commands
    app = NagaApp(Path(cwd), overrides=overrides, profile=profile, model=model,
                  on_event=(lambda e: on_event(event_to_json(e))) if on_event else None)
    await app.start()
    try:
        if not app.provider_status[0]:
            raise RuntimeError(f"model provider unavailable: {app.provider_status[1]}")
        app.set_mode(mode)
        r = await app.run(prompt)
        return {"text": r.text, "stop_reason": r.stop_reason, "iterations": r.iterations, "tool_calls": r.tool_calls,
                "files_changed": r.files_changed, "validations": r.validations, "session": app.session.id}
    finally:
        await app.shutdown()


def run(prompt: str, cwd: str | Path = ".", **kw: Any) -> dict[str, Any]:
    """Synchronous wrapper around arun()."""
    return asyncio.run(arun(prompt, cwd, **kw))
