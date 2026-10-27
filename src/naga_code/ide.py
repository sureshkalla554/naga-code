"""Connection to an editor running the Naga extension (VS Code: editors/vscode).

The extension listens on 127.0.0.1 with a random per-window token and writes a lock file to
``<config>/ide/*.json`` (mode 0600). Naga connects to the window whose workspace contains the project (or to the
one that started it, via NAGA_IDE_PORT / NAGA_IDE_TOKEN) and can then read the selection and the editor's
diagnostics, and open proposed edits in the editor's diff view.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .config import config_home
from .security.commands import Risk
from .tools.patching import apply_hunks, parse_patch
from .tools.registry import Tool, ToolResult


class IDEClient:
    def __init__(self, port: int, token: str, info: dict | None = None):
        self.port, self.token, self.info = port, token, info or {}
        self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0,
                                         headers={"Authorization": f"Bearer {token}"})

    @property
    def name(self) -> str:
        return self.info.get("ide", "editor")

    async def call(self, route: str, body: dict | None = None) -> dict:
        r = await self._client.post(route, json=body or {})
        r.raise_for_status()
        return r.json()

    async def ping(self) -> bool:
        try:
            return bool((await self.call("/ping")).get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def diagnostics(self, path: str | None = None) -> list[dict]:
        return (await self.call("/diagnostics", {"path": path} if path else {})).get("diagnostics", [])

    async def context(self) -> dict:
        return await self.call("/context")

    async def open_diff(self, path: str, content: str, title: str = "") -> None:
        await self.call("/openDiff", {"path": path, "content": content, "title": title})

    async def aclose(self) -> None:
        await self._client.aclose()


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def candidates(project_root: Path) -> list[tuple[int, str, dict]]:
    """Editors to try: the one that launched us (env), then lock files whose workspace contains the project."""
    out: list[tuple[int, str, dict]] = []
    if os.environ.get("NAGA_IDE_PORT") and os.environ.get("NAGA_IDE_TOKEN"):
        out.append((int(os.environ["NAGA_IDE_PORT"]), os.environ["NAGA_IDE_TOKEN"], {"ide": "vscode", "via": "env"}))
    root = project_root.resolve()
    d = config_home() / "ide"
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []:
        try:
            info = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not _alive(info.get("pid")):
            f.unlink(missing_ok=True)   # stale: the editor window is gone
            continue
        folders = [Path(p).resolve() for p in info.get("workspaceFolders", [])]
        if any(root == w or w in root.parents or root in w.parents for w in folders):
            out.append((int(info["port"]), str(info["token"]), info))
    return out


async def connect(project_root: Path) -> IDEClient | None:
    for port, token, info in candidates(project_root):
        client = IDEClient(port, token, info)
        if await client.ping():
            return client
        await client.aclose()
    return None


def format_diagnostics(diags: list[dict], root: Path, limit: int = 30) -> str:
    rows = []
    for d in diags[:limit]:
        try:
            rel = str(Path(d["path"]).relative_to(root))
        except ValueError:
            rel = d["path"]
        rows.append(f"{rel}:{d['line']}:{d['column']}: {d['severity']}: {d['message']}"
                    + (f" [{d['source']}]" if d.get("source") else ""))
    more = f"\n... {len(diags) - limit} more" if len(diags) > limit else ""
    return "\n".join(rows) + more if rows else "no problems reported"


async def errors_after_edit(ide: IDEClient, root: Path, paths: list[str], delay_s: float = 1.0) -> str:
    """Errors/warnings the editor reports for just-edited files (give its language server a moment first)."""
    await asyncio.sleep(delay_s)
    found: list[dict] = []
    for p in paths:
        try:
            found += [d for d in await ide.diagnostics(str((root / p).resolve()))
                      if d["severity"] in ("error", "warning")]
        except (httpx.HTTPError, ValueError):
            return ""
    return format_diagnostics(found, root) if found else ""


def proposed_content(path: Path, preview_diff: str) -> str | None:
    """The file as it would look after an edit, rebuilt from the approval preview (a unified diff)."""
    try:
        old = path.read_text() if path.exists() else ""
        for fp in parse_patch(preview_diff):
            return apply_hunks(old, fp.hunks, str(path))
    except Exception:  # noqa: BLE001 - previews are best effort
        return None
    return None


def register_tools(registry, ide: IDEClient, root: Path) -> None:
    async def ide_diagnostics(ctx, a: dict[str, Any]) -> ToolResult:
        path = str((root / a["path"]).resolve()) if a.get("path") else None
        diags = await ide.diagnostics(path)
        diags = [d for d in diags if Path(d["path"]).resolve() == root or root in Path(d["path"]).resolve().parents]
        return ToolResult(True, format_diagnostics(diags, root), summary=f"{len(diags)} problem(s) from {ide.name}")

    async def ide_selection(ctx, a: dict[str, Any]) -> ToolResult:
        c = await ide.context()
        if not c.get("activeFile"):
            return ToolResult(True, "no file is open in the editor", summary="no active file")
        sel = c.get("selection")
        if not sel:
            return ToolResult(True, f"active file: {c['activeFile']} (nothing selected)", summary="no selection")
        return ToolResult(True, f"{c['activeFile']} lines {sel['startLine']}-{sel['endLine']}:\n{sel['text']}",
                          summary=f"selection {sel['startLine']}-{sel['endLine']}")

    registry.register(Tool(
        "ide_diagnostics", "Errors and warnings the user's editor (language servers, linters) reports, for one file "
                           "or the whole project. Use after editing to catch type errors and bad imports.",
        {"type": "object", "additionalProperties": False, "properties": {"path": {"type": "string"}}},
        ide_diagnostics, Risk.READ, category="search"), replace=True)
    registry.register(Tool(
        "ide_selection", "The file open in the user's editor and the text they selected (when they refer to "
                         "'this', 'the selection' or 'the open file').",
        {"type": "object", "additionalProperties": False, "properties": {}},
        ide_selection, Risk.READ, category="search"), replace=True)
