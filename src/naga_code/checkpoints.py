"""Task checkpoints: recoverable patches of agent-made changes (never destructive rollbacks)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import git as G
from .config import data_home
from .tools.patching import unified_diff

if TYPE_CHECKING:
    from .app import NagaApp


def _text(b: bytes | None) -> str:
    return b.decode("utf-8", errors="replace") if b is not None else ""


def agent_patch(app: "NagaApp") -> tuple[str, str, list[str]]:
    """(forward patch, reverse patch, files) covering every file the agent changed this session."""
    first_before: dict[str, bytes | None] = {}
    for c in app.workspace.changes:
        first_before.setdefault(c.path, c.before)
    fwd, rev = [], []
    for rel, before in first_before.items():
        p = app.workspace.root / rel
        current = p.read_bytes() if p.exists() else None
        if current == before:
            continue
        fwd.append(unified_diff(_text(before), _text(current), rel))
        rev.append(unified_diff(_text(current), _text(before), rel))
    return "".join(fwd), "".join(rev), [r for r in first_before]


def create(app: "NagaApp", label: str = "") -> Path:
    fwd, rev, files = agent_patch(app)
    d = data_home() / "checkpoints" / app.session.id
    d.mkdir(parents=True, exist_ok=True)
    n = len(app.session.task.checkpoints) + 1
    base = d / f"{n:03d}"
    base.with_suffix(".patch").write_text(fwd)
    base.with_suffix(".reverse.patch").write_text(rev)
    meta = {"n": n, "label": label, "ts": time.time(), "files": files, "git": G.status(app.project_root).short(),
            "patch": str(base.with_suffix(".patch")), "reverse_patch": str(base.with_suffix(".reverse.patch")),
            "commands": app.session.task.commands[-20:], "validations": app.session.task.validations[-10:]}
    base.with_suffix(".json").write_text(json.dumps(meta, indent=2, default=str))
    app.session.task.checkpoints.append(meta)
    app.save()
    return base.with_suffix(".patch")
