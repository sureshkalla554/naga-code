"""Append-only JSONL audit log with secret redaction."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .redact import redact


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        return redact(v)[:4000]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **data: Any) -> None:
        if not self.path:
            return
        entry = {"ts": round(time.time(), 3), "event": event, **_clean(data)}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # auditing must never crash the agent
