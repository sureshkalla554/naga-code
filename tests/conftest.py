from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from naga_code.app import NagaApp
from naga_code.providers.fake import FakeProvider
from naga_code.security.permissions import ApprovalResponse, Choice


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Never touch the real ~/.config/naga or ~/.local/share/naga during tests."""
    home = tmp_path_factory.mktemp("naga-home")
    monkeypatch.setenv("NAGA_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (root / ".gitignore").write_text("ignored_dir/\n*.log\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


class Approver:
    """Scripted approval handler that records requests."""

    def __init__(self, *choices: Choice | ApprovalResponse):
        self.choices = list(choices)
        self.requests = []

    async def __call__(self, req):
        self.requests.append(req)
        c = self.choices.pop(0) if self.choices else Choice.APPROVE_ONCE
        return c if isinstance(c, ApprovalResponse) else ApprovalResponse(c)


@pytest_asyncio.fixture
async def make_app(project):
    apps = []

    async def factory(script=None, approver=None, overrides=None, native_tools=True, root=None, **kw):
        events = []
        app = NagaApp(root or project, provider=FakeProvider(script or [], native_tools=native_tools),
                      approval_handler=approver if approver is not None else Approver(),
                      on_event=events.append, overrides=overrides, **kw)
        app.events = events  # type: ignore[attr-defined]
        await app.start()
        apps.append(app)
        return app

    yield factory
    for a in apps:
        await a.shutdown()
