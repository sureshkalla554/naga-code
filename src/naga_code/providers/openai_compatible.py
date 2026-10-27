"""OpenAI-compatible chat completions provider.

Used for local servers that expose ``/v1/chat/completions`` (vLLM, llama.cpp's
``llama-server``, LM Studio, LocalAI, ...). The endpoint is configurable; an API
key is only sent when one is configured explicitly.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from .base import (GenerationOptions, Message, ModelInfo, Provider, ProviderError, StreamEvent,
                   ToolCall, Usage, explain_backend_error)


class OpenAICompatibleProvider(Provider):
    kind = "openai_compatible"

    def __init__(self, name: str, base_url: str, api_key: str | None = None, timeout: float = 600.0):
        super().__init__(name, base_url, timeout)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), headers=headers)

    def _url(self, path: str) -> str:
        base = self.base_url
        if not base.endswith("/v1"):
            base += "/v1"
        return base + path

    async def health(self) -> tuple[bool, str]:
        try:
            r = await self._client.get(self._url("/models"), timeout=3.0)
            r.raise_for_status()
            return True, f"{self.kind} server at {self.base_url}"
        except httpx.HTTPError as e:
            return False, f"{self.kind} server not reachable at {self.base_url}: {e.__class__.__name__}"

    async def list_models(self) -> list[ModelInfo]:
        try:
            r = await self._client.get(self._url("/models"), timeout=10.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot list models at {self.base_url}: {e}") from e
        return [ModelInfo(name=m["id"], provider=self.name,
                          context_length=m.get("max_model_len") or m.get("context_length"))
                for m in r.json().get("data", [])]

    @staticmethod
    def _encode(messages: list[Message]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            calls = [c for c in m.tool_calls if c.parse_error is None]
            if calls:
                d["tool_calls"] = [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                   for c in calls]
            if m.role == "tool":
                d["tool_call_id"] = m.tool_call_id
            out.append(d)
        return out

    async def stream_chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                          options: GenerationOptions | None = None) -> AsyncIterator[StreamEvent]:
        o = options or GenerationOptions()
        body: dict[str, Any] = {"model": model, "messages": self._encode(messages), "stream": True,
                                "stream_options": {"include_usage": True}}
        for k, v in (("temperature", o.temperature), ("top_p", o.top_p), ("max_tokens", o.max_tokens),
                     ("seed", o.seed)):
            if v is not None:
                body[k] = v
        body.update(o.extra)
        if tools:
            body["tools"] = tools
        t0 = time.monotonic()
        pending: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish = None
        try:
            async with self._client.stream("POST", self._url("/chat/completions"), json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode(errors="replace")
                    raise ProviderError(f"{self.kind} error {r.status_code}: {text[:500]}", explain_backend_error(text))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage"):
                        usage.prompt_tokens = chunk["usage"].get("prompt_tokens")
                        usage.completion_tokens = chunk["usage"].get("completion_tokens")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield StreamEvent(type="thinking", text=reasoning)
                        if delta.get("content"):
                            yield StreamEvent(type="text", text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = pending.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.ConnectError as e:
            raise ProviderError(f"cannot connect to {self.base_url}", "Is the local inference server running?") from e
        for idx in sorted(pending):
            slot = pending[idx]
            err = None
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
                if not isinstance(args, dict):
                    err, args = "tool arguments must be a JSON object", {}
            except json.JSONDecodeError as e:
                err, args = f"invalid JSON arguments: {e}", {}
            yield StreamEvent(type="tool_call", tool_call=ToolCall(
                name=slot["name"], arguments=args, parse_error=err, **({"id": slot["id"]} if slot["id"] else {})))
        usage.duration_s = time.monotonic() - t0
        yield StreamEvent(type="done", usage=usage, finish_reason=finish)

    async def aclose(self) -> None:
        await self._client.aclose()


class LlamaCppProvider(OpenAICompatibleProvider):
    """llama.cpp ``llama-server``: OpenAI-compatible chat plus /health and /props."""

    kind = "llama_cpp"

    def _root(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    async def health(self) -> tuple[bool, str]:
        try:
            r = await self._client.get(self._root() + "/health", timeout=3.0)
            if r.status_code == 503:
                return False, "llama.cpp server is up but still loading the model"
            r.raise_for_status()
            return True, f"llama.cpp server at {self.base_url}"
        except httpx.HTTPError as e:
            return False, f"llama.cpp server not reachable at {self.base_url}: {e.__class__.__name__}"

    async def list_models(self) -> list[ModelInfo]:
        models = await super().list_models()
        try:
            r = await self._client.get(self._root() + "/props", timeout=5.0)
            n_ctx = r.json().get("default_generation_settings", {}).get("n_ctx")
            for m in models:
                m.context_length = m.context_length or n_ctx
        except (httpx.HTTPError, ValueError):
            pass
        return models
