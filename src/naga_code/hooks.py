"""Opt-in lifecycle hooks.

Config (user ``~/.config/naga/config.toml`` or project ``.naga/config.toml``)::

    [[hooks]]
    event = "after_file_modification"   # see EVENTS
    command = "ruff format {path}"      # {path} / {tool} are shell-quoted placeholders
    match = "*.py"                      # optional glob on path (file events) or tool name (tool events)
    timeout = 30

Semantics:
- Hooks are visible: every run is reported to the UI and the audit log.
- ``before_*`` hooks may block the action by exiting with status 2 (stderr is the reason).
- Project hooks come from the repository and are untrusted: they do not run until the user trusts
  the exact hook definitions with ``/hooks trust`` (re-trust is required after any change).
- Hooks never trigger other hooks (no recursion), run with the scrubbed environment and a timeout,
  and cannot approve tool calls.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import data_home

EVENTS = ("session_start", "before_planning", "before_tool", "after_tool", "before_file_modification",
          "after_file_modification", "before_tests", "after_tests", "before_final_response", "session_end")


@dataclass
class HookSpec:
    event: str
    command: str
    match: str | None = None
    timeout: float = 30
    source: str = "user"

    @classmethod
    def parse(cls, d: dict[str, Any], source: str) -> "HookSpec":
        if d.get("event") not in EVENTS:
            raise ValueError(f"unknown hook event '{d.get('event')}' (valid: {', '.join(EVENTS)})")
        if not isinstance(d.get("command"), str) or not d["command"].strip():
            raise ValueError("hook needs a 'command' string")
        return cls(d["event"], d["command"], d.get("match"), float(d.get("timeout", 30)), source)


def _digest(hooks: list[dict]) -> str:
    return hashlib.sha256(json.dumps(hooks, sort_keys=True).encode()).hexdigest()


class HookEngine:
    def __init__(self, user_hooks: list[dict], project_hooks: list[dict], project_root: Path, terminal,
                 notify: Callable[[str, str], None] | None = None, audit=None):
        self.terminal = terminal
        self.notify = notify or (lambda k, m: None)
        self.audit = audit
        self.project_root = project_root
        self.errors: list[str] = []
        self.user: list[HookSpec] = self._parse(user_hooks, "user")
        self.project_raw = project_hooks
        self.project: list[HookSpec] = self._parse(project_hooks, "project")
        self._running = False

    def _parse(self, hooks: list[dict], source: str) -> list[HookSpec]:
        out = []
        for h in hooks or []:
            try:
                out.append(HookSpec.parse(h, source))
            except ValueError as e:
                self.errors.append(f"{source} hook ignored: {e}")
        return out

    @property
    def _trust_file(self) -> Path:
        return data_home() / "trusted_hooks.json"

    def project_trusted(self) -> bool:
        if not self.project_raw:
            return True
        try:
            trusted = json.loads(self._trust_file.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return trusted.get(str(self.project_root)) == _digest(self.project_raw)

    def trust_project(self) -> None:
        try:
            trusted = json.loads(self._trust_file.read_text())
        except (OSError, json.JSONDecodeError):
            trusted = {}
        trusted[str(self.project_root)] = _digest(self.project_raw)
        self._trust_file.parent.mkdir(parents=True, exist_ok=True)
        self._trust_file.write_text(json.dumps(trusted, indent=2))

    def active(self) -> list[HookSpec]:
        return self.user + (self.project if self.project_trusted() else [])

    async def run(self, event: str, payload: dict[str, Any]) -> str | None:
        """Run hooks for an event. Returns a block reason if a before_* hook vetoed the action."""
        if self._running:  # hooks cannot trigger hooks
            return None
        specs = [h for h in self.active() if h.event == event]
        if not specs:
            return None
        subject = str(payload.get("path") or payload.get("tool") or "")
        self._running = True
        try:
            for h in specs:
                if h.match and not fnmatch.fnmatch(subject, h.match) and not fnmatch.fnmatch(Path(subject).name, h.match):
                    continue
                cmd = h.command.replace("{path}", shlex.quote(str(payload.get("path", "")))) \
                               .replace("{tool}", shlex.quote(str(payload.get("tool", ""))))
                env = {"NAGA_HOOK_EVENT": event, "NAGA_HOOK_PAYLOAD": json.dumps(payload, default=str)[:20000]}
                self.notify("hook", f"{event}: {cmd}")
                res = await self.terminal.run(cmd, timeout=h.timeout, env=env, max_output=20000)
                if self.audit:
                    self.audit.record("hook", hook_event=event, command=cmd, source=h.source, exit_code=res.exit_code)
                if res.timed_out:
                    self.notify("warning", f"hook timed out after {h.timeout:.0f}s: {cmd}")
                elif res.exit_code == 2 and event.startswith("before_"):
                    return (res.stderr or res.stdout).strip()[:500] or f"hook '{cmd}' blocked the action"
                elif res.exit_code != 0:
                    self.notify("warning", f"hook failed (exit {res.exit_code}): {cmd}\n{(res.stderr or res.stdout)[-500:]}")
        finally:
            self._running = False
        return None
