"""Provider abstraction shared by every local inference backend."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])
    # Set when the model emitted something tool-shaped that could not be parsed.
    # Such calls are reported back to the model as errors and never executed.
    parse_error: str | None = None
    source: Literal["native", "text"] = "native"


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name, for role == "tool"
    meta: dict[str, Any] = field(default_factory=dict)  # never sent to the model

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        calls = [ToolCall(**c) for c in d.get("tool_calls", [])]
        return cls(role=d["role"], content=d.get("content", ""), tool_calls=calls,
                   tool_call_id=d.get("tool_call_id"), name=d.get("name"), meta=d.get("meta", {}))


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_s: float | None = None


@dataclass
class StreamEvent:
    type: Literal["text", "thinking", "tool_call", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None


@dataclass
class ModelInfo:
    name: str
    provider: str
    context_length: int | None = None
    capabilities: list[str] = field(default_factory=list)  # e.g. completion, tools, vision, thinking
    size_bytes: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def supports_tools(self) -> bool | None:
        return ("tools" in self.capabilities) if self.capabilities else None


@dataclass
class GenerationOptions:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    num_ctx: int | None = None
    seed: int | None = None
    think: bool | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """A provider failure with an actionable hint for the user."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n  hint: {self.hint}" if self.hint else base


def explain_backend_error(text: str) -> str | None:
    """Map common CUDA / memory / compatibility errors to a useful hint."""
    t = text.lower()
    if "out of memory" in t or "cuda error: out of memory" in t or "cudamalloc" in t:
        return ("GPU ran out of memory. Use a smaller model or quantization, lower num_ctx in the "
                "profile, or unload other models (`ollama ps` / `ollama stop <model>`).")
    if "model requires more system memory" in t:
        return "Not enough RAM to load this model. Pick a smaller model or close other processes."
    if "not found" in t and "model" in t:
        return "Model is not installed locally. Run `naga models` and pull it with `ollama pull <model>`."
    if "does not support tools" in t:
        return "This model has no native tool support; set tool_mode = \"text\" in its profile."
    if "cuda" in t and ("driver" in t or "version" in t):
        return "CUDA driver/runtime mismatch. Check `nvidia-smi` and the backend's CUDA build."
    if "unsupported" in t and "architecture" in t:
        return "The backend does not support this model architecture; try a different backend or model."
    return None


class Provider(ABC):
    kind: str = "base"

    def __init__(self, name: str, base_url: str, timeout: float = 600.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """Return (reachable, human-readable detail)."""

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def model_info(self, model: str) -> ModelInfo | None:
        for m in await self.list_models():
            if m.name == model:
                return m
        return None

    @abstractmethod
    def stream_chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def complete(self, model: str, messages: list[Message],
                       options: GenerationOptions | None = None) -> str:
        """Non-tool convenience call that collects the streamed text."""
        out: list[str] = []
        async for ev in self.stream_chat(model, messages, None, options):
            if ev.type == "text":
                out.append(ev.text)
        return "".join(out)

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
