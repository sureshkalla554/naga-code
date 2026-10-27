"""Local inference providers and the factory that builds them from configuration."""

from __future__ import annotations

import os
from typing import Any

from .base import (GenerationOptions, Message, ModelInfo, Provider, ProviderError, StreamEvent, ToolCall,
                   Usage)
from .ollama import OllamaProvider
from .openai_compatible import LlamaCppProvider, OpenAICompatibleProvider
from .simple_http import SimpleHTTPProvider

PROVIDER_KINDS = {
    "ollama": OllamaProvider,
    "llama_cpp": LlamaCppProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "vllm": OpenAICompatibleProvider,
    "simple_http": SimpleHTTPProvider,
}


def build_provider(name: str, spec: dict[str, Any]) -> Provider:
    kind = spec.get("kind", "openai_compatible")
    if kind == "fake":
        from .fake import FakeProvider
        return FakeProvider(name=name)
    if kind == "transformers":
        raise ProviderError("the 'transformers' provider kind is not implemented yet",
                            "Serve the model with llama.cpp, vLLM or Ollama and use that provider kind.")
    cls = PROVIDER_KINDS.get(kind)
    if cls is None:
        raise ProviderError(f"unknown provider kind '{kind}'", f"Use one of: {', '.join(PROVIDER_KINDS)}")
    kwargs: dict[str, Any] = {"name": name, "base_url": spec["base_url"], "timeout": spec.get("timeout", 600.0)}
    api_key = spec.get("api_key")
    if spec.get("api_key_env"):   # the key itself stays in the environment, never in the config file
        api_key = os.environ.get(spec["api_key_env"])
        if not api_key:
            raise ProviderError(f"provider '{name}' needs the API key in ${spec['api_key_env']}, which is not set",
                                f"export {spec['api_key_env']}=... before starting naga")
    if api_key:
        kwargs["api_key"] = api_key
    if cls is OllamaProvider and "keep_alive" in spec:
        kwargs["keep_alive"] = spec["keep_alive"] or None
    if cls is SimpleHTTPProvider:
        kwargs.update(path=spec.get("path", "/generate"), input_field=spec.get("input_field", "prompt"),
                      model=spec.get("model", name), context_length=spec.get("context_length"))
        return cls(**kwargs)
    return cls(**kwargs)


__all__ = ["GenerationOptions", "Message", "ModelInfo", "Provider", "ProviderError", "StreamEvent", "ToolCall",
           "Usage", "build_provider", "PROVIDER_KINDS"]
