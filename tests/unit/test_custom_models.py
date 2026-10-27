import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from naga_code import custom_models as cm
from naga_code.commands import REGISTRY
from naga_code.config import load_config
from naga_code.providers.base import Message
from naga_code.providers.simple_http import SimpleHTTPProvider, extract_text

from test_commands_extra import UI, run


def serve(routes):
    """Start a throwaway HTTP server; routes maps (method, path) -> fn(body) -> (status, content_type, payload)."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"null") if n else None
            calls.append((method, self.path, body, dict(self.headers)))
            fn = routes.get((method, self.path)) or (lambda b: (404, "application/json", {"detail": "Not Found"}))
            status, ctype, payload = fn(body)
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], calls


def root(_):
    return 200, "application/json", {"ok": True}


def openapi_generate(_):
    return 200, "application/json", {"openapi": "3.1.0", "paths": {
        "/health": {"get": {}},
        "/generate": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Req"}}}}}}},
        "components": {"schemas": {"Req": {"properties": {"message": {"type": "string"}, "max_tokens": {}}}}}}


@pytest.fixture
def fastapi_like():
    replies = []

    def generate(body):
        replies.append(body)
        text = body["message"]
        if "Reply with exactly: OK" in text:
            return 200, "application/json", {"response": "OK"}
        if '<tool_result name="read_file">' in text:
            return 200, "application/json", {"response": "The file says hello."}
        return 200, "application/json", {"response": '<tool_call>{"name": "read_file", "arguments": {"path": "hello.txt"}}</tool_call>'}

    srv, port, calls = serve({("GET", "/"): root, ("GET", "/openapi.json"): openapi_generate, ("POST", "/generate"): generate})
    yield port, replies
    srv.shutdown()


@pytest.fixture
def openai_like():
    def models(_):
        return 200, "application/json", {"object": "list", "data": [{"id": "my-llm", "max_model_len": 16384}]}

    def chat(body):
        if body.get("tools"):
            return 400, "application/json", {"error": "tools not supported"}
        sse = 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'
        return 200, "text/event-stream", sse

    srv, port, _ = serve({("GET", "/"): root, ("GET", "/v1/models"): models, ("POST", "/v1/chat/completions"): chat})
    yield port
    srv.shutdown()


def test_extract_text_shapes():
    assert extract_text({"response": "a"}) == "a"
    assert extract_text([{"generated_text": "b"}]) == "b"
    assert extract_text({"choices": [{"message": {"content": "c"}}]}) == "c"
    assert extract_text({"output": {"text": "d"}}) == "d"
    assert extract_text({"nothing": 1}) is None


async def test_detects_fastapi_generate_endpoint_and_field(fastapi_like):
    port, _ = fastapi_like
    d = await cm.detect(str(port))
    assert (d.kind, d.path, d.input_field, d.native_tools) == ("simple_http", "/generate", "message", False)


async def test_detects_openai_server_and_tool_support(openai_like):
    d = await cm.detect(f"127.0.0.1:{openai_like}")
    assert d.kind == "openai_compatible" and d.models == ["my-llm"] and d.context_length == 16384
    assert d.native_tools is False   # the server rejected `tools`, so Naga will use text tool calls


async def test_nothing_listening_is_a_clear_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ValueError, match="nothing is answering"):
        await cm.detect(str(port))


async def test_plain_text_endpoint_with_explicit_path():
    srv, port, calls = serve({("GET", "/"): root, ("POST", "/my/infer"): lambda b: (200, "text/plain", "pong")})
    try:
        d = await cm.detect(str(port), "/my/infer")
        p = SimpleHTTPProvider("t", d.base_url, d.path, d.input_field, "m")
        assert await p.complete("m", [Message("user", "ping")]) == "pong"
        assert calls[-1][2]["prompt"].endswith("User: ping\n\nAssistant:")
        await p.aclose()
    finally:
        srv.shutdown()


async def test_custom_model_command_end_to_end(make_app, fastapi_like, project):
    port, replies = fastapi_like
    (project / "hello.txt").write_text("hello\n")
    app = await make_app()
    app._provider_override = None   # use real providers built from config for this test
    ui = UI(app, picks=["yes"])
    await run(ui, app, f"/custom-model mygen {port}")
    assert app.profile_name == "mygen" and app.tool_mode == "text"
    assert any("test reply" in o and "OK" in o for o in ui.out)
    cfg = load_config(app.project_root)
    assert cfg.get("general.default_profile") == "mygen"
    assert cfg.section("providers")["custom-mygen"]["path"] == "/generate"
    # a real agent turn through the custom server: text tool call -> tool result -> final answer
    result = await app.run("what is in hello.txt?")
    assert result.text == "The file says hello." and result.tool_calls == 1
    assert '<tool_result name="read_file">' in replies[-1]["message"] and "hello" in replies[-1]["message"]
    await run(UI(app), app, "/custom-model list")
    await run(UI(app), app, "/custom-model remove mygen")
    cfg = load_config(app.project_root)
    assert "mygen" not in cfg.section("profiles") and cfg.get("general.default_profile") != "mygen"
    assert app.profile_name != "mygen"


async def test_custom_model_name_rules(make_app):
    app = await make_app()
    ui = UI(app)
    assert await REGISTRY.dispatch(ui, app, "/custom-model coding 8000")
    assert ui.errors and "built-in" in ui.errors[0]


async def test_probes_common_paths_without_openapi():
    srv, port, calls = serve({("GET", "/"): root, ("GET", "/generate"): lambda b: (405, "text/plain", "no"),
                              ("POST", "/generate"): lambda b: (200, "application/json", {"text": "OK"}) if "text" in b
                              else (422, "application/json", {"detail": "field 'text' required"})})
    try:
        d = await cm.detect(str(port))
        assert (d.kind, d.path, d.input_field) == ("simple_http", "/generate", "text")
    finally:
        srv.shutdown()


def test_parse_target_forms():
    t = cm.parse_target("8000")
    assert (t.url, t.remote) == ("http://127.0.0.1:8000", False)
    t = cm.parse_target("gpu-box:8000")
    assert (t.base, t.path, t.remote) == ("http://gpu-box:8000", None, True)
    t = cm.parse_target("https://llm.example.com/v1/chat/completions")
    assert (t.base, t.hint, t.remote) == ("https://llm.example.com", "openai", True)
    t = cm.parse_target("https://example.com/proxy/mymodel/v1/")
    assert (t.base, t.hint) == ("https://example.com/proxy/mymodel", "openai")
    t = cm.parse_target("http://localhost:9000/api/generate")
    assert (t.base, t.hint, t.remote) == ("http://localhost:9000", "ollama", False)
    t = cm.parse_target("http://127.0.0.1:9000/my/infer")
    assert (t.base, t.path) == ("http://127.0.0.1:9000", "/my/infer")
    with pytest.raises(ValueError):
        cm.parse_target("ftp://x")


async def test_full_endpoint_url_and_prefixed_api():
    srv, port, _ = serve({("GET", "/"): root, ("GET", "/my/infer"): lambda b: (405, "text/plain", ""),
                          ("POST", "/my/infer"): lambda b: (200, "application/json", {"output": "OK"}),
                          ("GET", "/team/llm/v1/models"): lambda b: (200, "application/json", {"data": [{"id": "x"}]}),
                          ("POST", "/team/llm/v1/chat/completions"): lambda b: (400, "application/json", {})})
    try:
        d = await cm.detect(f"http://127.0.0.1:{port}/my/infer")
        assert (d.kind, d.base_url, d.path) == ("simple_http", f"http://127.0.0.1:{port}", "/my/infer")
        d = await cm.detect(f"http://127.0.0.1:{port}/team/llm")
        assert (d.kind, d.base_url, d.models) == ("openai_compatible", f"http://127.0.0.1:{port}/team/llm", ["x"])
    finally:
        srv.shutdown()


async def test_api_key_from_environment(monkeypatch, make_app):
    def models(_):
        ok = calls and calls[-1][3].get("Authorization") == "Bearer s3cret"
        return (200, "application/json", {"data": [{"id": "secure-llm"}]}) if ok else (401, "application/json", {})

    srv, port, calls = serve({("GET", "/"): root, ("GET", "/v1/models"): models})
    try:
        with pytest.raises(ValueError, match="--api-key-env"):
            await cm.detect(str(port))
        d = await cm.detect(str(port), headers={"Authorization": "Bearer s3cret"})
        assert d.models == ["secure-llm"]
        from naga_code.providers import ProviderError, build_provider
        spec = {"kind": "openai_compatible", "base_url": d.base_url, "api_key_env": "NAGA_TEST_KEY"}
        with pytest.raises(ProviderError, match="NAGA_TEST_KEY"):
            build_provider("x", spec)
        monkeypatch.setenv("NAGA_TEST_KEY", "s3cret")
        p = build_provider("x", spec)
        assert [m.name for m in await p.list_models()] == ["secure-llm"]
        await p.aclose()
        app = await make_app()
        app._provider_override = None
        ui = UI(app, picks=["no"])
        await run(ui, app, f"/custom-model sec {port} --api-key-env NAGA_TEST_KEY")
        saved = load_config(app.project_root).section("providers")["custom-sec"]
        assert saved["api_key_env"] == "NAGA_TEST_KEY" and "s3cret" not in json.dumps(saved)
    finally:
        srv.shutdown()


async def test_remote_url_needs_confirmation(make_app):
    app = await make_app()
    ui = UI(app, confirms=[False])
    await run(ui, app, "/custom-model far https://llm.example.invalid/v1")
    assert "far" not in load_config(app.project_root).section("profiles")
