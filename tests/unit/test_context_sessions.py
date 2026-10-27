from naga_code.agent.context import ContextBudget, ContextManager
from naga_code.memory.sessions import SessionStore
from naga_code.memory.storage import MemoryStore
from naga_code.providers.base import Message, ToolCall
from naga_code.providers.fake import FakeProvider


def long_history(n=12):
    h = []
    for i in range(n):
        h.append(Message("user", f"request {i}: must keep constraint C{i}"))
        c = ToolCall("read_file", {"path": f"f{i}.py"})
        h.append(Message("assistant", "", [c]))
        h.append(Message("tool", "x" * 3000, tool_call_id=c.id, name="read_file"))
        h.append(Message("assistant", f"answer {i}"))
    return h


async def test_compaction_summarizes_head_and_keeps_tail():
    cm = ContextManager(ContextBudget(8000, 1000, 0.75))
    hist = long_history()
    fake = FakeProvider(["- user wanted C0..C9\n- files f0..f9 read"])
    new, note = await cm.compact(hist, fake, "m")
    assert "compacted" in note and new[0].meta["summary"] and "C0" in new[0].content
    assert new[2].role == "user"  # tail starts at a real user message
    assert new[-1].content == "answer 11"
    assert len(new) < len(hist)
    prompt = fake.calls[0]["messages"]
    assert "acceptance criteria" in prompt[0]["content"] and "request 0" in prompt[1]["content"]


async def test_compaction_falls_back_when_model_fails():
    class Broken(FakeProvider):
        async def complete(self, *a, **k):
            raise RuntimeError("down")
    cm = ContextManager(ContextBudget(8000, 1000, 0.75))
    new, note = await cm.compact(long_history(), Broken(), "m")
    assert "deterministic" in note and "request 0" in new[0].content


def test_prepare_elides_old_tool_outputs_without_mutating_history():
    cm = ContextManager(ContextBudget(32000), keep_recent_tool_results=2)
    hist = long_history(12)   # 10 old outputs -> one batch of 8 elided
    out = cm.prepare(hist)
    tools = [m for m in out if m.role == "tool"]
    assert "elided" in tools[0].content and "elided" not in tools[-1].content
    assert sum("elided" in t.content for t in tools) == 8
    assert all("elided" not in m.content for m in hist)


def test_prepare_keeps_earlier_messages_identical_between_calls():
    """Adding one tool result must not change any earlier message (backend prompt-prefix cache)."""
    cm = ContextManager(ContextBudget(32000), keep_recent_tool_results=2)
    changed = 0
    for n in range(3, 20):
        before, after = cm.prepare(long_history(n)), cm.prepare(long_history(n + 1))
        if [m.content for m in after[:len(before)]] != [m.content for m in before]:
            changed += 1
    assert changed <= 2   # only when a whole batch is elided (n+1 = 10 and 18)


def test_estimation_and_calibration():
    cm = ContextManager(ContextBudget(10000, 1000, 0.5))
    msgs = [Message("user", "x" * 36000)]
    assert 9000 < cm.estimate(msgs) < 11000 and cm.needs_compaction(cm.estimate(msgs))
    cm.calibrate(36000, 12000)
    assert cm.chars_per_token < 3.6


def test_session_roundtrip_and_resume(project, isolated_home):
    store = SessionStore(project)
    s = store.new()
    c = ToolCall("read_file", {"path": "a"})
    s.messages = [Message("user", "first request"), Message("assistant", "", [c]),
                  Message("tool", "out", tool_call_id=c.id, name="read_file")]
    s.task.plan.objective = "obj"
    s.task.record_command("pytest", 0, 1.0, "test")
    store.save(s)
    loaded = store.load(store.resolve_id(s.id[:12]))
    assert loaded.title == "first request" and loaded.messages[1].tool_calls[0].arguments == {"path": "a"}
    assert loaded.task.plan.objective == "obj" and loaded.task.commands[0]["command"] == "pytest"
    assert store.latest().id == s.id


async def test_app_resume_restores_history(make_app):
    app = await make_app(["hello there"])
    await app.run("hi")
    sid = app.session.id
    app.reset()
    assert app.agent.history == []
    app.resume(sid)
    assert [m.content for m in app.agent.history] == ["hi", "hello there"]


def test_memory_requires_no_secrets_and_supports_edit_delete(project):
    m = MemoryStore(project)
    e = m.add("use pnpm, not npm")
    assert "use pnpm" in m.prompt()
    assert m.edit("project", e.id, "use pnpm 9")
    try:
        m.add("my key is sk-ant-abcdefghijklmnopqrstuvwxyz")
        raise AssertionError("secret stored")
    except ValueError:
        pass
    assert m.remove("project", e.id) and not m.all()


async def test_remember_tool_requires_approval(make_app):
    from conftest import Approver

    from naga_code.security.permissions import Choice
    approver = Approver(Choice.DENY)
    app = await make_app([{"text": "", "tool_calls": [("remember", {"text": "tests need DOCKER=1"})]}, "ok"],
                         approver=approver)
    await app.run("remember that")
    assert approver.requests and approver.requests[0].tool == "remember"
    assert app.memory.all() == []
