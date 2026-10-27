"""Provider for home-made model servers: one POST endpoint that takes a prompt and returns text.

Typical Python wrappers look like ``POST /generate {"prompt": "..."} -> {"response": "..."}``. The request
field (``prompt``, ``message``, ``messages``, ``text``, ``input``, ...) is configurable, and the reply text is
taken from the first common field found (``response``, ``text``, ``output``, ``generated_text``,
``choices[0].message.content``, ...), or the raw body if it is not JSON. These servers have no tool-calling API,
so profiles using this provider use Naga's text tool protocol.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from .base import GenerationOptions, Message, ModelInfo, Provider, ProviderError, StreamEvent, Usage

TEXT_KEYS = ("response", "text", "output", "generated_text", "completion", "answer", "content", "result", "reply",
             "message", "generation", "outputs", "data")


def extract_text(payload: Any, depth: int = 0) -> str | None:
    """Find the generated text in a JSON reply of unknown shape."""
    if depth > 4 or payload is None:
        return None
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return extract_text(payload[0], depth + 1) if payload else None
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            c = choices[0]
            if isinstance(c, dict):
                msg = c.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"]
                if isinstance(c.get("text"), str):
                    return c["text"]
        for k in TEXT_KEYS:
            if k in payload:
                found = extract_text(payload[k], depth + 1)
                if found is not None:
                    return found
    return None


def render_prompt(messages: list[Message]) -> str:
    """Flatten a chat into a plain prompt for servers that take a single string."""
    names = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool result"}
    parts = [f"{names.get(m.role, m.role)}: {m.content}" for m in messages if m.content]
    return "\n\n".join(parts) + "\n\nAssistant:"


class SimpleHTTPProvider(Provider):
    kind = "simple_http"

    def __init__(self, name: str, base_url: str, path: str = "/generate", input_field: str = "prompt",
                 model: str = "custom", context_length: int | None = None, timeout: float = 600.0,
                 api_key: str | None = None):
        super().__init__(name, base_url, timeout)
        self.path = "/" + path.lstrip("/")
        self.input_field = input_field
        self.model = model
        self.context_length = context_length
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), headers=headers)

    async def health(self) -> tuple[bool, str]:
        try:
            await self._client.get(self.base_url + "/", timeout=3.0)  # any HTTP answer means the server is up
            return True, f"custom server at {self.base_url}{self.path}"
        except httpx.HTTPError as e:
            return False, f"custom server not reachable at {self.base_url}: {e.__class__.__name__}"

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name=self.model, provider=self.name, context_length=self.context_length,
                          capabilities=["completion"])]

    async def model_info(self, model: str) -> ModelInfo | None:
        return (await self.list_models())[0]

    def _body(self, messages: list[Message], o: GenerationOptions) -> dict[str, Any]:
        if self.input_field == "messages":
            body: dict[str, Any] = {"messages": [{"role": m.role if m.role != "tool" else "user",
                                                  "content": m.content} for m in messages]}
        else:
            body = {self.input_field: render_prompt(messages)}
        if o.max_tokens is not None:
            body["max_new_tokens"] = body["max_tokens"] = o.max_tokens
        if o.temperature is not None:
            body["temperature"] = o.temperature
        body.update(o.extra)
        return body

    async def stream_chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                          options: GenerationOptions | None = None) -> AsyncIterator[StreamEvent]:
        o = options or GenerationOptions()
        body = self._body(messages, o)
        t0 = time.monotonic()
        try:
            r = await self._client.post(self.base_url + self.path, json=body)
        except httpx.ConnectError as e:
            raise ProviderError(f"cannot connect to {self.base_url}", "Is your model server still running?") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"request to {self.base_url}{self.path} failed: {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise ProviderError(f"custom server refused the request ({r.status_code})",
                                "Check the API key (custom models read it from the variable set with --api-key-env).")
        if r.status_code >= 400:
            raise ProviderError(f"custom server error {r.status_code}: {r.text[:500]}",
                                f"Check that {self.path} accepts JSON with a '{self.input_field}' field.")
        try:
            text = extract_text(r.json())
        except (json.JSONDecodeError, ValueError):
            text = r.text
        if text is None:
            raise ProviderError(f"could not find the generated text in the reply from {self.path}: {r.text[:300]}",
                                "Return JSON like {\"response\": \"...\"} or plain text.")
        prompt = body.get(self.input_field)
        if isinstance(prompt, str) and text.startswith(prompt):
            text = text[len(prompt):]   # e.g. Hugging Face pipelines echo the prompt in generated_text
        yield StreamEvent(type="text", text=text.strip())
        yield StreamEvent(type="done", usage=Usage(duration_s=time.monotonic() - t0))

    async def aclose(self) -> None:
        await self._client.aclose()
