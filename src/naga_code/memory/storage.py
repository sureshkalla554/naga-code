"""User-approved persistent memory (preferences and durable facts), stored locally as JSON.

Nothing is stored without an explicit approval; entries that look like secrets are refused.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import config_home, data_home
from ..security.permissions import ApprovalRequest, Level
from ..security.redact import contains_secret
from ..tools.context import ToolContext
from ..tools.registry import Tool, ToolRegistry, ToolResult
from .sessions import project_slug


@dataclass
class MemoryEntry:
    id: int
    text: str
    scope: str  # user | project
    created: float


class MemoryStore:
    def __init__(self, project_root: Path | None):
        self.paths = {"user": config_home() / "memory.json"}
        if project_root:
            self.paths["project"] = data_home() / "projects" / project_slug(project_root) / "memory.json"

    def _load(self, scope: str) -> list[MemoryEntry]:
        p = self.paths.get(scope)
        if not p or not p.is_file():
            return []
        try:
            return [MemoryEntry(**e) for e in json.loads(p.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save(self, scope: str, entries: list[MemoryEntry]) -> None:
        p = self.paths[scope]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([asdict(e) for e in entries], indent=2))

    def all(self) -> list[MemoryEntry]:
        return [e for s in self.paths for e in self._load(s)]

    def add(self, text: str, scope: str = "project") -> MemoryEntry:
        text = text.strip()
        if not text:
            raise ValueError("memory text is empty")
        if contains_secret(text):
            raise ValueError("refusing to store something that looks like a secret or credential")
        if scope not in self.paths:
            raise ValueError(f"unknown memory scope '{scope}'")
        entries = self._load(scope)
        e = MemoryEntry(max((x.id for x in entries), default=0) + 1, text[:1000], scope, time.time())
        entries.append(e)
        self._save(scope, entries)
        return e

    def remove(self, scope: str, entry_id: int) -> bool:
        entries = self._load(scope)
        kept = [e for e in entries if e.id != entry_id]
        if len(kept) == len(entries):
            return False
        self._save(scope, kept)
        return True

    def edit(self, scope: str, entry_id: int, text: str) -> bool:
        if contains_secret(text):
            raise ValueError("refusing to store something that looks like a secret or credential")
        entries = self._load(scope)
        for e in entries:
            if e.id == entry_id:
                e.text = text.strip()[:1000]
                self._save(scope, entries)
                return True
        return False

    def clear(self, scope: str) -> None:
        if scope in self.paths:
            self._save(scope, [])

    def export(self) -> dict[str, Any]:
        return {s: [asdict(e) for e in self._load(s)] for s in self.paths}

    def prompt(self) -> str:
        return "\n".join(f"- ({e.scope}) {e.text}" for e in self.all())


async def remember(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    store: MemoryStore = ctx.services["memory"]
    try:
        e = store.add(a["text"], a.get("scope", "project"))
    except ValueError as err:
        return ToolResult.error(str(err))
    return ToolResult(True, f"remembered (#{e.id}, {e.scope}): {e.text}", summary="memory saved")


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "remember", "Ask the user to save a durable preference or project fact to persistent memory (e.g. 'use pnpm', "
                    "'tests need DOCKER=1'). Never store secrets. The user must approve each entry.",
        {"type": "object", "additionalProperties": False, "required": ["text"],
         "properties": {"text": {"type": "string"}, "scope": {"enum": ["user", "project"]}}},
        remember, Level.WRITE, category="memory",
        assess=lambda ctx, a: ApprovalRequest("remember", f"save to {a.get('scope', 'project')} memory: {a['text']}",
                                              Level.WRITE, str(ctx.workspace.root),
                                              impact="stored locally and added to future sessions' context")))
