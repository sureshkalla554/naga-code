"""Ollama native API provider (/api/chat, /api/tags, /api/show, /api/ps)."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from .base import (GenerationOptions, Message, ModelInfo, Provider, ProviderError, StreamEvent,
                   ToolCall, Usage, explain_backend_error)


class OllamaProvider(Provider):
    kind = "ollama"

    def __init__(self, name: str = "ollama", base_url: str = "http://127.0.0.1:11434", timeout: float = 600.0,
                 api_key: str | None = None):
        super().__init__(name, base_url, timeout)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}   # e.g. Ollama behind a proxy
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=5.0),
                                         headers=headers)
        self._show_cache: dict[str, ModelInfo] = {}

    async def health(self) -> tuple[bool, str]:
        try:
            r = await self._client.get("/api/version", timeout=3.0)
            r.raise_for_status()
            return True, f"Ollama {r.json().get('version', '?')} at {self.base_url}"
        except httpx.HTTPError as e:
            return False, f"Ollama not reachable at {self.base_url}: {e.__class__.__name__}"

    async def list_models(self) -> list[ModelInfo]:
        try:
            r = await self._client.get("/api/tags", timeout=10.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot list Ollama models: {e}", "Is `ollama serve` running?") from e
        out = []
        for m in r.json().get("models", []):
            d = m.get("details", {})
            out.append(ModelInfo(name=m["name"], provider=self.name, context_length=d.get("context_length"),
                                 capabilities=list(m.get("capabilities") or []), size_bytes=m.get("size"),
                                 details={k: d.get(k) for k in ("family", "parameter_size", "quantization_level")}))
        return out

    async def model_info(self, model: str) -> ModelInfo | None:
        if model in self._show_cache:
            return self._show_cache[model]
        try:
            r = await self._client.post("/api/show", json={"model": model}, timeout=15.0)
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot query model '{model}': {e}") from e
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        mi = d.get("model_info", {}) or {}
        arch = mi.get("general.architecture")
        ctx = mi.get(f"{arch}.context_length") if arch else None
        info = ModelInfo(name=model, provider=self.name, context_length=ctx,
                         capabilities=list(d.get("capabilities") or []),
                         details={"architecture": arch, **(d.get("details") or {})})
        self._show_cache[model] = info
        return info

    async def running(self) -> list[dict[str, Any]]:
        r = await self._client.get("/api/ps", timeout=5.0)
        r.raise_for_status()
        return r.json().get("models", [])

    @staticmethod
    def _encode(messages: list[Message]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                d["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
                                   if c.parse_error is None]
            if m.role == "tool" and m.name:
                d["tool_name"] = m.name
            images = m.meta.get("images")
            if images:
                d["images"] = images
            out.append(d)
        return out

    @staticmethod
    def _options(o: GenerationOptions | None) -> dict[str, Any]:
        if not o:
            return {}
        opts = {"temperature": o.temperature, "top_p": o.top_p, "num_predict": o.max_tokens,
                "num_ctx": o.num_ctx, "seed": o.seed, **o.extra}
        return {k: v for k, v in opts.items() if v is not None}

    async def stream_chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                          options: GenerationOptions | None = None) -> AsyncIterator[StreamEvent]:
        body: dict[str, Any] = {"model": model, "messages": self._encode(messages), "stream": True,
                                "options": self._options(options)}
        if tools:
            body["tools"] = tools
        if options and options.think is not None:
            body["think"] = options.think
        t0 = time.monotonic()
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode(errors="replace")
                    try:
                        text = json.loads(text).get("error", text)
                    except json.JSONDecodeError:
                        pass
                    raise ProviderError(f"Ollama error {r.status_code}: {text}", explain_backend_error(text))
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ProviderError(f"Ollama error: {chunk['error']}", explain_backend_error(chunk["error"]))
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        yield StreamEvent(type="thinking", text=msg["thinking"])
                    if msg.get("content"):
                        yield StreamEvent(type="text", text=msg["content"])
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        err = None
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError as e:
                                err, args = f"invalid JSON arguments: {e}", {}
                        if not isinstance(args, dict):
                            err, args = "tool arguments must be a JSON object", {}
                        yield StreamEvent(type="tool_call", tool_call=ToolCall(
                            name=fn.get("name", ""), arguments=args, parse_error=err,
                            **({"id": tc["id"]} if tc.get("id") else {})))
                    if chunk.get("done"):
                        yield StreamEvent(type="done", finish_reason=chunk.get("done_reason"), usage=Usage(
                            prompt_tokens=chunk.get("prompt_eval_count"),
                            completion_tokens=chunk.get("eval_count"),
                            duration_s=time.monotonic() - t0))
                        return
        except httpx.ConnectError as e:
            raise ProviderError(f"cannot connect to Ollama at {self.base_url}", "Start it with `ollama serve`.") from e
        except httpx.ReadTimeout as e:
            raise ProviderError("Ollama response timed out", "The model may still be loading; retry or raise timeout.") from e

    async def aclose(self) -> None:
        await self._client.aclose()
