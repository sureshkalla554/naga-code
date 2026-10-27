import json

import httpx
import pytest

from naga_code.providers import ProviderError, build_provider
from naga_code.providers.base import GenerationOptions, Message, ToolCall, explain_backend_error
from naga_code.providers.ollama import OllamaProvider
from naga_code.providers.openai_compatible import LlamaCppProvider, OpenAICompatibleProvider


def mock_client(handler, base_url="http://x"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


async def collect(gen):
    return [e async for e in gen]


async def test_ollama_streams_text_tool_calls_and_usage():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        lines = [
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo", "thinking": "hmm"}, "done": False},
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 120, "eval_count": 7},
        ]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

    p = OllamaProvider()
    p._client = mock_client(handler)
    msgs = [Message("system", "s"), Message("user", "hi"),
            Message("assistant", "", [ToolCall("read_file", {"path": "x"}, id="c1")]),
            Message("tool", "content", tool_call_id="c1", name="read_file")]
    events = await collect(p.stream_chat("m", msgs, [{"type": "function"}], GenerationOptions(num_ctx=4096, temperature=0.1)))
    assert "".join(e.text for e in events if e.type == "text") == "Hello"
    assert [e.text for e in events if e.type == "thinking"] == ["hmm"]
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a.py"}
    done = events[-1]
    assert done.type == "done" and done.usage.prompt_tokens == 120
    body = seen["body"]
    assert body["options"] == {"num_ctx": 4096, "temperature": 0.1}
    assert body["messages"][3] == {"role": "tool", "content": "content", "tool_name": "read_file"}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"


async def test_ollama_error_has_hint():
    p = OllamaProvider()
    p._client = mock_client(lambda r: httpx.Response(500, json={"error": "CUDA error: out of memory"}))
    with pytest.raises(ProviderError) as ei:
        await collect(p.stream_chat("m", [Message("user", "x")]))
    assert "GPU ran out of memory" in str(ei.value)


async def test_ollama_invalid_json_arguments_flagged_not_executed():
    def handler(r):
        return httpx.Response(200, text=json.dumps({"message": {"tool_calls": [
            {"function": {"name": "x", "arguments": "{not json"}}]}, "done": False}) + "\n" +
            json.dumps({"done": True}))
    p = OllamaProvider()
    p._client = mock_client(handler)
    calls = [e.tool_call for e in await collect(p.stream_chat("m", [Message("user", "x")])) if e.type == "tool_call"]
    assert calls[0].parse_error and "invalid JSON" in calls[0].parse_error


async def test_openai_compatible_accumulates_streamed_tool_call_deltas():
    chunks = [
        {"choices": [{"delta": {"content": "ok "}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "run_", "arguments": "{\"comm"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "command", "arguments": "and\": \"ls\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 5}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    p = OpenAICompatibleProvider("v", "http://x/v1")
    p._client = mock_client(lambda r: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    events = await collect(p.stream_chat("m", [Message("user", "x")], [{"type": "function"}]))
    call = next(e.tool_call for e in events if e.type == "tool_call")
    assert call.name == "run_command" and call.arguments == {"command": "ls"} and call.id == "call_1"
    assert events[-1].usage.prompt_tokens == 50 and events[-1].finish_reason == "tool_calls"


async def test_openai_compatible_unreachable_health():
    def boom(r):
        raise httpx.ConnectError("refused")
    p = OpenAICompatibleProvider("v", "http://127.0.0.1:9/v1")
    p._client = mock_client(boom)
    ok, detail = await p.health()
    assert not ok and "not reachable" in detail


async def test_llamacpp_reads_context_from_props():
    def handler(r: httpx.Request):
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen.gguf"}]})
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    p = LlamaCppProvider("l", "http://x")
    p._client = mock_client(handler)
    [m] = await p.list_models()
    assert m.context_length == 16384


def test_build_provider_unknown_kind_and_transformers_message():
    with pytest.raises(ProviderError):
        build_provider("x", {"kind": "nope", "base_url": "http://x"})
    with pytest.raises(ProviderError) as ei:
        build_provider("x", {"kind": "transformers", "base_url": ""})
    assert "not implemented" in str(ei.value)


def test_error_hints():
    assert "smaller model" in explain_backend_error("model requires more system memory (40 GiB)")
    assert explain_backend_error("all good") is None
