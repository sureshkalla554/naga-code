import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from naga_code.commands import REGISTRY
from naga_code.remote_approval import Notifier, PhoneApprover
from naga_code.security.commands import Risk
from naga_code.security.permissions import ApprovalRequest, Choice

from test_commands_extra import UI, run


class FakeNtfy:
    """Minimal ntfy: POST / (JSON publish), POST /<topic> (plain text publish), GET /<topic>/json?poll=1&since=."""

    def __init__(self):
        self.messages: list[dict] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                if self.path == "/":
                    msg = json.loads(raw)
                else:
                    msg = {"topic": self.path.strip("/"), "message": raw}
                msg["time"] = int(time.time())
                srv.messages.append(msg)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                u = urlparse(self.path)
                topic = u.path.strip("/").removesuffix("/json")
                since = int(parse_qs(u.query).get("since", ["0"])[0])
                body = "\n".join(json.dumps(m) for m in srv.messages if m["topic"] == topic and m["time"] >= since)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def phone_taps(self, button: str, delay: float = 0.3, nonce: str | None = None):
        """Simulate tapping a notification button: POST its body to its URL (like the ntfy app does)."""
        def tap():
            time.sleep(delay)
            req = [m for m in self.messages if m.get("actions")][-1]
            action = next(a for a in req["actions"] if a["label"] == button)
            body = action["body"] if nonce is None else f"{button.lower()} {nonce}"
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(action["url"], data=body.encode(), method="POST"))
        threading.Thread(target=tap, daemon=True).start()


@pytest.fixture
def ntfy():
    s = FakeNtfy()
    yield s
    s.httpd.shutdown()


def request(level=Risk.EXECUTE):
    return ApprovalRequest("run_command", "pytest -q", level, "/proj", reason="run the tests", impact="runs pytest")


async def test_phone_approve_and_deny(ntfy, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    approver = PhoneApprover(Notifier(ntfy.url, "naga-secret", timeout_s=10))
    ntfy.phone_taps("Approve")
    assert (await approver(request())).choice == Choice.APPROVE_ONCE
    published = [m for m in ntfy.messages if m.get("actions")][-1]
    assert "pytest -q" in published["message"] and published["topic"] == "naga-secret"
    ntfy.phone_taps("Deny")
    resp = await approver(request())
    assert resp.choice == Choice.DENY and resp.note == "denied on phone"


async def test_wrong_nonce_is_ignored_and_timeout_denies(ntfy, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    approver = PhoneApprover(Notifier(ntfy.url, "naga-secret", timeout_s=1.5))
    ntfy.phone_taps("Approve", nonce="0000000000000000")   # a stale/forged reply
    resp = await approver(request())
    assert resp.choice == Choice.DENY and "no answer" in resp.note


async def test_dangerous_actions_are_never_sent_to_the_phone(ntfy):
    approver = PhoneApprover(Notifier(ntfy.url, "naga-secret", timeout_s=1))
    resp = await approver(request(Risk.DANGEROUS))
    assert resp.choice == Choice.DENY and not ntfy.messages


async def test_notify_command_setup_and_test(ntfy, make_app):
    app = await make_app()
    ui = UI(app)
    await run(ui, app, f"/notify setup {ntfy.url}")
    topic = app.config.get("notify.ntfy_topic")
    assert topic.startswith("naga-") and len(topic) > 20
    await run(ui, app, "/notify test")
    assert ntfy.messages[-1]["topic"] == topic and ntfy.messages[-1]["title"] == "Naga test"
    await run(ui, app, "/notify off")
    assert not app.config.get("notify.ntfy_topic")
    assert REGISTRY.resolve("notify") is not None


_real_sleep = asyncio.sleep


async def _fast_sleep(s):
    await _real_sleep(min(s, 0.1))
