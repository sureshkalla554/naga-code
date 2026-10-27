"""Minimal Model Context Protocol client (JSON-RPC 2.0) over stdio and Streamable HTTP.

Implements: initialize handshake with version negotiation, tools/list (paginated), tools/call,
resources/list and prompts/list when the server advertises them, ping, timeouts and clean shutdown.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import __version__
from ..security.sandbox import scrub_env

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


class MCPError(Exception):
    pass


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
        self.command, self.args, self.env, self.cwd = command, args, env or {}, cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        # The server gets a scrubbed environment plus only the variables configured for it.
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=scrub_env(extra={k: os.path.expandvars(v) for k, v in self.env.items()}),
            cwd=self.cwd, start_new_session=True, limit=16 * 1024 * 1024)
        self._reader = asyncio.ensure_future(self._read_loop())
        asyncio.ensure_future(self._stderr_loop())

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self.stderr_tail = (self.stderr_tail + [line.decode(errors="replace").rstrip()])[-20:]

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while line := await self.proc.stdout.readline():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers must not write non-JSON to stdout; ignore noise
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("server closed the connection" +
                                           (": " + " | ".join(self.stderr_tail[-3:]) if self.stderr_tail else "")))
        self._pending.clear()

    async def send(self, msg: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise MCPError("server process is not running")
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, msg: dict[str, Any], timeout: float) -> dict[str, Any]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg["id"]] = fut
        await self.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg["id"], None)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self.proc.wait(), 2)
            except (asyncio.TimeoutError, OSError):
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()


class HttpTransport:
    """Streamable HTTP: POST JSON-RPC, response as JSON or an SSE stream; session id via Mcp-Session-Id."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = {k: os.path.expandvars(v) for k, v in (headers or {}).items()}
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))

    async def start(self) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def send(self, msg: dict[str, Any]) -> None:
        r = await self.client.post(self.url, json=msg, headers=self._headers())
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {r.text[:200]}")

    async def request(self, msg: dict[str, Any], timeout: float) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            async with self.client.stream("POST", self.url, json=msg, headers=self._headers()) as r:
                if r.status_code >= 400:
                    raise MCPError(f"HTTP {r.status_code}: {(await r.aread()).decode(errors='replace')[:200]}")
                if r.headers.get("mcp-session-id"):
                    self.session_id = r.headers["mcp-session-id"]
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    data: list[str] = []
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line.strip() and data:
                            obj = json.loads("\n".join(data))
                            data = []
                            if obj.get("id") == msg["id"]:
                                return obj
                    raise MCPError("SSE stream ended without a response")
                return json.loads(await r.aread())
        return await asyncio.wait_for(go(), timeout)

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass
        await self.client.aclose()


class MCPClient:
    def __init__(self, name: str, transport: StdioTransport | HttpTransport, timeout: float = 20):
        self.name = name
        self.transport = transport
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.tools: list[MCPTool] = []

    async def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        resp = await self.transport.request(msg, timeout or self.timeout)
        if "error" in resp:
            err = resp["error"]
            raise MCPError(f"{method} failed: {err.get('message')} (code {err.get('code')})")
        return resp.get("result", {})

    async def connect(self) -> None:
        await self.transport.start()
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
            "clientInfo": {"name": "naga-code", "version": __version__}})
        version = result.get("protocolVersion")
        if version not in PROTOCOL_VERSIONS:
            raise MCPError(f"server speaks unsupported protocol version {version!r}")
        self.protocol_version = version
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol_version = version
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {})
        await self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if "tools" in self.capabilities:
            self.tools = await self.list_tools()

    async def list_tools(self) -> list[MCPTool]:
        tools, cursor = [], None
        for _ in range(50):
            res = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            for t in res.get("tools", []):
                tools.append(MCPTool(t["name"], t.get("description", ""), t.get("inputSchema") or {"type": "object"},
                                     t.get("annotations") or {}))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120) -> tuple[bool, str]:
        res = await self._request("tools/call", {"name": name, "arguments": arguments}, timeout)
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource", {}))[:4000])
            else:
                parts.append(f"[{c.get('type')} content omitted]")
        if res.get("structuredContent") and not parts:
            parts.append(json.dumps(res["structuredContent"]))
        return not res.get("isError", False), "\n".join(parts)

    async def list_resources(self) -> list[dict[str, Any]]:
        if "resources" not in self.capabilities:
            return []
        return (await self._request("resources/list", {})).get("resources", [])

    async def list_prompts(self) -> list[dict[str, Any]]:
        if "prompts" not in self.capabilities:
            return []
        return (await self._request("prompts/list", {})).get("prompts", [])

    async def ping(self) -> bool:
        try:
            await self._request("ping", timeout=5)
            return True
        except (MCPError, asyncio.TimeoutError):
            return False

    async def close(self) -> None:
        await self.transport.close()
