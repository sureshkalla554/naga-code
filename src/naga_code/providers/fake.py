"""Deterministic scripted provider for tests and offline demos (no GPU, no network)."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Union

from .base import GenerationOptions, Message, ModelInfo, Provider, StreamEvent, ToolCall, Usage

# A scripted reply: plain text, {"text": str, "tool_calls": [(name, args), ...]}, or a callable
# receiving the conversation and returning either of those.
Reply = Union[str, dict[str, Any], Callable[[list[Message]], Union[str, dict[str, Any]]]]


class FakeProvider(Provider):
    kind = "fake"

    def __init__(self, script: list[Reply] | None = None, name: str = "fake",
                 context_length: int = 32768, native_tools: bool = True, delay: float = 0.0):
        super().__init__(name, "fake://local")
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self.context_length = context_length
        self.native_tools = native_tools
        self.delay = delay

    async def health(self) -> tuple[bool, str]:
        return True, "fake provider"

    async def list_models(self) -> list[ModelInfo]:
        caps = ["completion", "tools"] if self.native_tools else ["completion"]
        return [ModelInfo(name="fake-model", provider=self.name, context_length=self.context_length,
                          capabilities=caps)]

    async def model_info(self, model: str) -> ModelInfo | None:
        info = (await self.list_models())[0]
        info.name = model
        return info

    async def stream_chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                          options: GenerationOptions | None = None) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": [m.to_dict() for m in messages], "tools": tools})
        reply: Reply = self.script.pop(0) if self.script else "(fake provider: script exhausted)"
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, str):
            reply = {"text": reply}
        text = reply.get("text", "")
        for i in range(0, len(text), 16):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield StreamEvent(type="text", text=text[i:i + 16])
        for name, args in reply.get("tool_calls", []):
            yield StreamEvent(type="tool_call", tool_call=ToolCall(name=name, arguments=args))
        prompt_chars = sum(len(m.content) for m in messages)
        yield StreamEvent(type="done", usage=Usage(prompt_tokens=prompt_chars // 4,
                                                   completion_tokens=len(text) // 4, duration_s=0.0))
