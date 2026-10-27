"""Persist a session's file-change ledger so /undo and /rewind still work after Naga restarts.

Layout: ``<data>/changes/<project-slug>/<session>/index.json`` plus ``blobs/<sha256>`` (zlib-compressed file
contents, stored once per distinct version). Very large files are recorded without contents: they can't be
restored automatically, and /rewind says so instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from pathlib import Path

from .config import data_home
from .memory.sessions import project_slug
from .workspace import ChangeRecord

MAX_BLOB = 5_000_000


def _dir(project_root: Path, session_id: str) -> Path:
    return data_home() / "changes" / project_slug(project_root) / session_id


def _put(blobs: Path, data: bytes | None) -> str | None:
    if data is None:
        return None
    if len(data) > MAX_BLOB:
        return "too-large"
    sha = hashlib.sha256(data).hexdigest()
    f = blobs / sha
    if not f.exists():
        blobs.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(zlib.compress(data, 6))
        os.replace(tmp, f)
    return sha


def _get(blobs: Path, ref: str | None) -> bytes | None:
    if ref is None:
        return None
    if ref == "too-large":
        raise ValueError("file too large to keep a copy")
    return zlib.decompress((blobs / ref).read_bytes())


def save(project_root: Path, session_id: str, changes: list[ChangeRecord]) -> None:
    d = _dir(project_root, session_id)
    if not changes and not d.exists():
        return
    blobs = d / "blobs"
    rows = [{"id": c.id, "path": c.path, "tool": c.tool, "task": c.task_id, "agent": c.agent,
             "before": _put(blobs, c.before), "after": _put(blobs, c.after)} for c in changes]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "index.json.tmp"
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, d / "index.json")


def load(project_root: Path, session_id: str) -> list[ChangeRecord]:
    d = _dir(project_root, session_id)
    try:
        rows = json.loads((d / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for r in rows:
        try:
            out.append(ChangeRecord(r["id"], r["path"], _get(d / "blobs", r["before"]), _get(d / "blobs", r["after"]),
                                    r["tool"], r.get("task"), r.get("agent", "main")))
        except (OSError, ValueError, zlib.error):
            break   # stop at the first change we can't restore: later ones depend on it
    return out
