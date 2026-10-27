"""Live tests against a local Ollama server. Run with: uv run pytest -m integration

Model: NAGA_TEST_MODEL (default qwen3-coder:latest). Skipped when Ollama or the model is unavailable.
"""

import os

import httpx
import pytest

from naga_code.app import NagaApp
from naga_code.providers.base import GenerationOptions, Message
from naga_code.providers.ollama import OllamaProvider

pytestmark = pytest.mark.integration
MODEL = os.environ.get("NAGA_TEST_MODEL", "qwen3-coder:latest")


def _available() -> bool:
    try:
        tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(m["name"] == MODEL for m in tags.get("models", []))


skip = pytest.mark.skipif(not _available(), reason=f"Ollama with {MODEL} not available")


@skip
async def test_streaming_response():
    p = OllamaProvider()
    chunks = [e async for e in p.stream_chat(MODEL, [Message("user", "Reply with exactly: pong")],
                                             options=GenerationOptions(num_ctx=4096, temperature=0))]
    text = "".join(e.text for e in chunks if e.type == "text")
    assert "pong" in text.lower()
    assert sum(1 for e in chunks if e.type == "text") >= 1 and chunks[-1].type == "done"
    assert chunks[-1].usage.prompt_tokens
    await p.aclose()


@skip
async def test_agent_fixes_bug_with_real_model(project):
    from conftest import Approver
    app = NagaApp(project, approval_handler=Approver(), model=MODEL,
                  overrides={"profiles": {"coding": {"num_ctx": 16384}}})
    await app.start()
    try:
        res = await app.run("test_calc.py fails. Fix the bug in calc.py, then run `python3 -m pytest -q` to confirm.")
    finally:
        await app.shutdown()
    assert "a + b" in (project / "calc.py").read_text()
    assert res.stop_reason == "completed"
    assert any(v["verdict"] == "PASSED" for v in app.session.task.validations)
