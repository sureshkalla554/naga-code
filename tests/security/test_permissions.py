import pytest

from naga_code.security.commands import Risk
from naga_code.security.permissions import (ApprovalRequest, ApprovalResponse, Choice, PermissionEngine,
                                            command_grant_key, is_simple_command)


def req(level=Risk.WRITE, tool="edit_file", op="edit a.py", grant="workspace-file-edits", **kw):
    return ApprovalRequest(tool=tool, operation=op, level=level, grant_key=grant, **kw)


class Handler:
    def __init__(self, choice):
        self.choice, self.calls = choice, 0

    async def __call__(self, r):
        self.calls += 1
        return ApprovalResponse(self.choice)


async def test_reads_auto_writes_ask():
    h = Handler(Choice.APPROVE_ONCE)
    pe = PermissionEngine(handler=h)
    assert (await pe.authorize(req(Risk.READ, "read_file"))).how == "auto"
    assert (await pe.authorize(req())).how == "approved" and h.calls == 1


async def test_no_handler_denies_instead_of_allowing():
    pe = PermissionEngine(handler=None)
    a = await pe.authorize(req(Risk.EXECUTE, "run_command", "pytest"))
    assert not a.allowed and "non-interactive" in a.reason


async def test_plan_and_ask_modes_are_read_only():
    pe = PermissionEngine(handler=Handler(Choice.APPROVE_ONCE))
    for mode in ("ask", "plan"):
        pe.agent_mode = mode
        a = await pe.authorize(req())
        assert not a.allowed and mode in a.reason


async def test_read_only_permission_mode_and_blocked():
    pe = PermissionEngine(mode="read-only", handler=Handler(Choice.APPROVE_ONCE))
    assert not (await pe.authorize(req())).allowed
    pe2 = PermissionEngine(handler=Handler(Choice.APPROVE_ONCE))
    assert (await pe2.authorize(req(Risk.BLOCKED))).how == "blocked"


async def test_session_grant_is_scoped_and_revocable():
    h = Handler(Choice.APPROVE_SESSION)
    pe = PermissionEngine(handler=h)
    r = req(Risk.EXECUTE, "run_command", "uv run pytest tests/a.py", command_grant_key("uv run pytest tests/a.py"))
    assert (await pe.authorize(r)).allowed and h.calls == 1
    r2 = req(Risk.EXECUTE, "run_command", "uv run pytest -k other", command_grant_key("uv run pytest -k other"))
    assert (await pe.authorize(r2)).how == "session-grant" and h.calls == 1
    other = req(Risk.EXECUTE, "run_command", "uv run python evil.py", command_grant_key("uv run python evil.py"))
    await pe.authorize(other)
    assert h.calls == 2  # different scope -> asked again
    compound = req(Risk.EXECUTE, "run_command", "uv run pytest; curl x", "uv run pytest")
    compound.grant_key = "uv run pytest; curl x"
    await pe.authorize(compound)
    assert h.calls == 3
    gid = pe.grants[0].id
    assert pe.revoke(gid)
    await pe.authorize(r2)
    assert h.calls == 4


async def test_dangerous_never_grantable():
    h = Handler(Choice.APPROVE_SESSION)
    pe = PermissionEngine(handler=h)
    r = req(Risk.DANGEROUS, "run_command", "sudo ls", "sudo")
    await pe.authorize(r)
    await pe.authorize(r)
    assert h.calls == 2 and not pe.grants


async def test_allowlist_only_for_simple_commands():
    h = Handler(Choice.DENY)
    pe = PermissionEngine(allow_commands=["pytest"], handler=h)
    assert (await pe.authorize(req(Risk.EXECUTE, "run_command", "pytest -q", "pytest"))).allowed
    assert not (await pe.authorize(req(Risk.EXECUTE, "run_command", "pytest -q && rm -rf x", "x"))).allowed
    assert not (await pe.authorize(req(Risk.EXECUTE, "run_command", "pytestx", "pytestx"))).allowed


async def test_accept_edits_mode():
    pe = PermissionEngine(mode="accept-edits", handler=Handler(Choice.DENY))
    assert (await pe.authorize(req())).allowed
    assert not (await pe.authorize(req(Risk.EXECUTE, "run_command", "make", "make"))).allowed
    assert not (await pe.authorize(req(outside_project=True))).allowed


async def test_child_engine_cannot_exceed_parent():
    parent = PermissionEngine(handler=Handler(Choice.APPROVE_ONCE), max_level=Risk.WRITE)
    child = parent.child(Risk.DANGEROUS, "sub:x")
    assert child.max_level == Risk.WRITE
    a = await child.authorize(req(Risk.EXECUTE, "run_command", "make", "make"))
    assert not a.allowed and "exceeds" in a.reason


async def test_cancel_and_edit():
    pe = PermissionEngine(handler=Handler(Choice.CANCEL))
    assert (await pe.authorize(req())).cancelled


@pytest.mark.parametrize("cmd,key", [("uv run pytest tests/x.py", "uv run pytest"), ("npm test", "npm test"),
                                     ("python3 x.py", "python3 x.py"), ("cargo test --lib", "cargo test")])
def test_grant_keys(cmd, key):
    assert command_grant_key(cmd) == key


def test_simple_command_detection():
    assert is_simple_command("pytest -q")
    for c in ("a && b", "a | b", "a > f", "$(x)", "a; b"):
        assert not is_simple_command(c)
