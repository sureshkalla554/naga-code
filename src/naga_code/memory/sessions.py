"""Session persistence: conversation history + task ledger saved after every turn."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.task_state import TaskState
from ..config import data_home
from ..providers.base import Message


def project_slug(root: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-") or "root"
    return f"{name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"


@dataclass
class Session:
    id: str
    project: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    title: str = ""
    model: str = ""
    mode: str = "agent"
    messages: list[Message] = field(default_factory=list)
    task: TaskState = field(default_factory=TaskState)
    summaries: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "project": self.project, "created": self.created, "updated": self.updated,
                "title": self.title, "model": self.model, "mode": self.mode,
                "messages": [m.to_dict() for m in self.messages], "task": self.task.to_dict(),
                "summaries": self.summaries, "usage": self.usage, "version": 1}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(id=d["id"], project=d["project"], created=d.get("created", 0), updated=d.get("updated", 0),
                   title=d.get("title", ""), model=d.get("model", ""), mode=d.get("mode", "agent"),
                   messages=[Message.from_dict(m) for m in d.get("messages", [])],
                   task=TaskState.from_dict(d["task"]) if d.get("task") else TaskState(),
                   summaries=d.get("summaries", []), usage=d.get("usage", {}))


class SessionStore:
    def __init__(self, project_root: Path, base: Path | None = None):
        self.project_root = project_root
        self.dir = (base or data_home() / "sessions") / project_slug(project_root)

    def new(self) -> Session:
        sid = time.strftime("%Y%m%d-%H%M%S-") + hashlib.sha1(os.urandom(8)).hexdigest()[:6]
        return Session(id=sid, project=str(self.project_root))

    def path(self, sid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
            raise ValueError(f"invalid session id '{sid}'")
        return self.dir / f"{sid}.json"

    def save(self, s: Session) -> Path:
        s.updated = time.time()
        if not s.title:
            first = next((m.content for m in s.messages if m.role == "user" and not m.meta.get("synthetic")), "")
            s.title = first.strip().splitlines()[0][:80] if first.strip() else ""
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(s.id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(s.to_dict(), default=str))
        os.replace(tmp, p)
        return p

    def load(self, sid: str) -> Session:
        return Session.from_dict(json.loads(self.path(sid).read_text()))

    def list(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"id": d["id"], "title": d.get("title", ""), "updated": d.get("updated", 0),
                        "messages": len(d.get("messages", [])), "model": d.get("model", "")})
        return sorted(out, key=lambda x: x["updated"], reverse=True)

    def latest(self) -> Session | None:
        rows = self.list()
        return self.load(rows[0]["id"]) if rows else None

    def resolve_id(self, prefix: str) -> str:
        ids = [r["id"] for r in self.list() if r["id"].startswith(prefix)]
        if not ids:
            raise KeyError(f"no session matching '{prefix}'")
        if len(ids) > 1 and prefix not in ids:
            raise KeyError(f"'{prefix}' is ambiguous: {', '.join(ids[:5])}")
        return prefix if prefix in ids else ids[0]

    def delete(self, sid: str) -> None:
        self.path(sid).unlink(missing_ok=True)
