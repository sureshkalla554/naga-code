import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from naga_code import ide
from naga_code.config import config_home
from naga_code.mentions import expand_mentions
from naga_code.tools.patching import unified_diff
from naga_code.workspace import Workspace


class FakeEditor:
    """Speaks the same protocol as editors/vscode/extension.js."""

    def __init__(self, folder, diagnostics=None):
        self.token = "t" * 48
        self.diagnostics = diagnostics or []
        self.opened = []
        editor = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                if self.headers.get("Authorization") != f"Bearer {editor.token}":
                    return self._send(401, {"error": "unauthorized"})
                routes = {"/ping": {"ok": True}, "/diagnostics": {"diagnostics": [
                    d for d in editor.diagnostics if not body.get("path") or d["path"] == body["path"]]},
                    "/context": {"activeFile": str(folder / "calc.py"),
                                 "selection": {"text": "return a - b", "startLine": 2, "endLine": 2}}}
                if self.path == "/openDiff":
                    editor.opened.append(body)
                    return self._send(200, {"opened": True})
                self._send(200, routes[self.path])

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        d = config_home() / "ide"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"vscode-{os.getpid()}-{self.port}.json").write_text(json.dumps(
            {"port": self.port, "token": self.token, "pid": os.getpid(), "ide": "vscode",
             "workspaceFolders": [str(folder)]}))


@pytest.fixture
def editor(project):
    e = FakeEditor(project, [{"path": str(project / "calc.py"), "line": 2, "column": 12, "severity": "error",
                              "source": "Pylance", "message": '"c" is not defined'}])
    yield e
    e.srv.shutdown()


async def test_discovers_editor_by_lock_file_and_rejects_bad_token(project, editor):
    client = await ide.connect(project)
    assert client is not None and client.port == editor.port
    await client.aclose()
    bad = ide.IDEClient(editor.port, "wrong-token")
    assert not await bad.ping()
    await bad.aclose()
    other = project.parent / "elsewhere"
    other.mkdir()
    assert await ide.connect(other) is None   # lock file is for a different workspace


async def test_editor_errors_are_attached_after_an_edit(make_app, project, editor):
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
              {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + c"})]},
              "Edited."]
    app = await make_app(script, overrides={"ide": {"diagnostics_delay_s": 0}})
    assert await app.connect_ide()
    await app.run("change it")
    edit_result = [m.content for m in app.agent.history if m.role == "tool" and m.name == "edit_file"][0]
    assert "The editor now reports" in edit_result and '"c" is not defined' in edit_result
    assert {"ide_diagnostics", "ide_selection"} <= set(app.registry.names())
    from naga_code.tools.registry import ToolCall
    sel = await app.registry.execute(ToolCall("ide_selection", {}), app.ctx)
    assert "return a - b" in sel.output


def test_mentions_attach_files_and_line_ranges(project):
    ws = Workspace(project)
    out = expand_mentions("look at @calc.py:2 and @test_calc.py please, mail me@example.com, @missing.py", ws)
    assert "--- calc.py (lines 2-2) ---" in out and "return a - b" in out
    assert "--- test_calc.py (whole file) ---" in out and "missing.py ---" not in out
    assert expand_mentions("no mentions here", ws) == "no mentions here"
    assert expand_mentions("see @../../etc/passwd", ws) == "see @../../etc/passwd"   # outside the project: ignored


def test_proposed_content_rebuilds_the_edited_file(project):
    old = (project / "calc.py").read_text()
    new = old.replace("a - b", "a + b")
    assert ide.proposed_content(project / "calc.py", unified_diff(old, new, "calc.py")) == new


PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


async def test_image_mentions_and_vision_check(make_app, project):
    (project / "shot.png").write_bytes(PNG_1PX)
    app = await make_app(["I see a tiny image.", "ok"])
    app.model_caps = ["completion", "tools", "vision"]
    await app.run("Look at @shot.png. What is in it?")
    user = [m for m in app.agent.history if m.role == "user"][0]
    assert len(user.meta.get("images", [])) == 1
    assert "[Files referenced" not in user.content            # images aren't pasted as text
    app.model_caps = ["completion", "tools"]                   # a model without vision
    await app.run("and now @shot.png ?")
    assert not [m for m in app.agent.history if m.role == "user"][1].meta.get("images")


async def test_image_command_queues_for_next_message(make_app, project):
    from test_commands_extra import UI, run
    (project / "shot.png").write_bytes(PNG_1PX)
    app = await make_app(["seen"])
    app.model_caps = ["vision"]
    await run(UI(app), app, f"/image {project / 'shot.png'}")
    assert len(app.pending_images) == 1
    await app.run("describe it")
    assert app.pending_images == [] and app.agent.history[0].meta.get("images")
