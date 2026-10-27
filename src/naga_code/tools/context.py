"""Shared state handed to every tool handler."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import Config
from ..security.audit import AuditLog
from ..security.permissions import PermissionEngine
from ..workspace import Workspace

if TYPE_CHECKING:
    from ..agent.task_state import TaskState
    from ..hooks import HookEngine
    from ..terminal import TerminalEngine


class ArtifactStore:
    """Large tool outputs are stored here and referenced by id instead of filling the context."""

    def __init__(self, directory: Path):
        self.dir = directory
        self._ids = itertools.count(1)
        self.index: dict[str, dict[str, Any]] = {}

    def save(self, text: str, label: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        aid = f"out-{next(self._ids)}"
        (self.dir / f"{aid}.txt").write_text(text, encoding="utf-8", errors="replace")
        self.index[aid] = {"label": label, "chars": len(text), "lines": text.count("\n") + 1}
        return aid

    def read(self, aid: str, offset: int = 0, limit: int = 200, pattern: str | None = None) -> str:
        path = self.dir / f"{aid}.txt"
        if aid not in self.index or not path.is_file():
            raise KeyError(f"unknown artifact '{aid}' (known: {', '.join(self.index) or 'none'})")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if pattern:
            rx = re.compile(pattern)
            hits = [f"{i + 1}: {ln}" for i, ln in enumerate(lines) if rx.search(ln)]
            return "\n".join(hits[offset:offset + limit]) or "(no matching lines)"
        chunk = lines[offset:offset + limit]
        return "\n".join(f"{offset + i + 1}: {ln}" for i, ln in enumerate(chunk))


@dataclass
class ToolContext:
    workspace: Workspace
    config: Config
    permissions: PermissionEngine
    terminal: "TerminalEngine"
    artifacts: ArtifactStore
    audit: AuditLog | None = None
    hooks: "HookEngine | None" = None
    task: "TaskState | None" = None
    agent_name: str = "main"
    services: dict[str, Any] = field(default_factory=dict)   # skills, subagents, mcp, memory ...
    notify_cb: Callable[[str, str], None] | None = None

    def notify(self, kind: str, message: str) -> None:
        if self.notify_cb:
            self.notify_cb(kind, message)

    def derive(self, **changes: Any) -> "ToolContext":
        values = {f: getattr(self, f) for f in self.__dataclass_fields__}
        values.update(changes)
        return ToolContext(**values)
