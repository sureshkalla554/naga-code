import asyncio

from conftest import Approver

from naga_code.security.permissions import Choice


def kinds(app):
    return [e.kind for e in app.events]


async def test_tool_results_are_returned_to_model(make_app):
    app = await make_app([{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]}, "The file subtracts."])
    res = await app.run("what does add do?")
    assert res.stop_reason == "completed" and res.text == "The file subtracts."
    second_call = app.agent.provider.calls[1]["messages"]
    tool_msg = [m for m in second_call if m["role"] == "tool"][0]
    assert "return a - b" in tool_msg["content"] and tool_msg["name"] == "read_file"
    assert app.agent.provider.calls[0]["tools"]  # native tool schemas were sent


async def test_full_fix_and_validate_cycle(make_app, project):
    script = [
        {"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
        {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})]},
        {"text": "", "tool_calls": [("run_tests", {"command": "python3 -m pytest -q"})]},
        "Fixed.",
    ]
    app = await make_app(script)
    res = await app.run("fix it")
    assert res.files_changed == ["calc.py"]
    assert res.validations and res.validations[-1]["verdict"] == "PASSED"
    assert "a + b" in (project / "calc.py").read_text()


async def test_validation_nudge_when_edits_unvalidated(make_app):
    script = [
        {"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
        {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})]},
        "Done!",
        "Done. Note: not validated.",
    ]
    app = await make_app(script)
    res = await app.run("fix it")
    assert res.text == "Done. Note: not validated."
    nudges = [m for m in app.agent.history if m.meta.get("synthetic") and "no tests" in m.content]
    assert len(nudges) == 1


async def test_repeated_failures_stop_the_loop(make_app):
    bad = {"text": "", "tool_calls": [("read_file", {"path": "missing.py"})]}
    app = await make_app([bad] * 10)
    res = await app.run("read missing")
    assert res.stop_reason == "repeated_failures"
    assert res.iterations <= 5


async def test_iteration_limit(make_app):
    loop = [{"text": "", "tool_calls": [("list_directory", {"path": ".", "depth": i % 3 + 1})]} for i in range(10)]
    app = await make_app(loop, overrides={"agent": {"max_iterations": 3}})
    res = await app.run("loop")
    assert res.stop_reason == "iteration_limit" and "limit" in res.text


async def test_repeated_final_summary_is_accepted(make_app):
    summary = "## Summary\n" + "I fixed the bug in calc.py and verified the result carefully with the test suite. " * 3
    script = [{"text": summary, "tool_calls": [("git_status", {})]},
              {"text": summary + " Again.", "tool_calls": [("git_status", {})]},
              {"text": summary + " Once more.", "tool_calls": [("git_status", {})]}]
    app = await make_app(script)
    res = await app.run("x")
    assert res.stop_reason == "completed" and res.text.startswith("## Summary")
    assert any("repeated its final summary" in e.text for e in app.events if e.kind == "info")


async def test_cancel_from_approval_prompt(make_app, project):
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
              {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})]}]
    app = await make_app(script, approver=Approver(Choice.CANCEL))
    res = await app.run("fix")
    assert res.stop_reason == "cancelled" and "a - b" in (project / "calc.py").read_text()
    calls = {c.id for m in app.agent.history for c in m.tool_calls}
    answered = {m.tool_call_id for m in app.agent.history if m.role == "tool"}
    assert calls <= answered  # history stays valid after cancellation


async def test_interrupted_task_mid_command(make_app):
    app = await make_app([{"text": "", "tool_calls": [("run_command", {"command": "sleep 30"})]}])
    task = asyncio.ensure_future(app.run("wait"))
    await asyncio.sleep(0.8)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    calls = {c.id for m in app.agent.history for c in m.tool_calls}
    answered = {m.tool_call_id for m in app.agent.history if m.role == "tool"}
    assert calls <= answered
    assert app.sessions.list()  # session persisted despite the interruption
    # the session can continue afterwards
    app.agent.provider.script.append("still here")
    res = await app.run("hello?")
    assert res.text == "still here"


async def test_text_tool_mode_for_models_without_native_tools(make_app):
    script = ['Let me read.\n<tool_call>{"name": "read_file", "arguments": {"path": "calc.py"}}</tool_call>',
              "It subtracts."]
    app = await make_app(script, native_tools=False)
    assert app.tool_mode == "text"
    res = await app.run("explain add")
    assert res.text == "It subtracts."
    sent = app.agent.provider.calls[1]
    assert sent["tools"] is None
    assert "<tool_call>" in sent["messages"][0]["content"]  # protocol described in system prompt
    assert any("<tool_result" in m["content"] for m in sent["messages"] if m["role"] == "user")


async def test_prose_mentioning_tools_is_not_executed(make_app, project):
    app = await make_app(["I ran write_file and created hack.py successfully."], native_tools=False)
    res = await app.run("x")
    assert res.tool_calls == 0 and not (project / "hack.py").exists()


async def test_plan_mode_blocks_writes(make_app, project):
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
              {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})]},
              {"text": "", "tool_calls": [("update_plan", {"objective": "fix add", "steps": [{"title": "edit calc.py"}],
                                                           "acceptance_criteria": ["test passes"]})]},
              "Plan ready."]
    app = await make_app(script)
    app.set_mode("plan")
    await app.run("plan the fix")
    assert "a - b" in (project / "calc.py").read_text()
    assert app.session.task.plan.objective == "fix add"
    assert "acceptance" in app.system_prompt().lower() and "test passes" in app.system_prompt()


async def test_provider_error_reported(make_app):
    app = await make_app()

    async def broken(*a, **k):
        from naga_code.providers import ProviderError
        raise ProviderError("boom", "hint here")
        yield  # pragma: no cover

    app.agent.provider.stream_chat = broken
    res = await app.run("x")
    assert res.stop_reason == "error" and "boom" in res.text


async def test_denials_trigger_one_nudge(make_app):
    from naga_code.security.permissions import Choice
    deny = [Choice.DENY] * 5
    cmds = [{"text": "", "tool_calls": [("run_command", {"command": f"python3 -c 'print({i})'"})]} for i in range(4)]
    app = await make_app(cmds + ["ok, stopping"], approver=Approver(*deny))
    res = await app.run("x")
    nudges = [m for m in app.agent.history if m.meta.get("synthetic") and "denied by the permission system" in m.content]
    assert len(nudges) == 1 and res.text == "ok, stopping"


async def test_run_command_tests_are_recorded_as_validation(make_app):
    app = await make_app([{"text": "", "tool_calls": [("run_command", {"command": "python3 -m pytest -q"})]}, "ran"])
    res = await app.run("run tests")
    assert res.validations and res.validations[0]["kind"] == "test"
    assert res.validations[0]["verdict"] in {"PASSED", "FAILED"}


async def test_backend_parse_error_is_retried(make_app):
    from naga_code.providers import ProviderError
    app = await make_app(["recovered"])
    real = app.agent.provider.stream_chat
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            async def boom():
                raise ProviderError("Ollama error 500: XML syntax error on line 13")
                yield  # pragma: no cover
            return boom()
        return real(*a, **k)

    app.agent.provider.stream_chat = flaky
    res = await app.run("x")
    assert res.stop_reason == "completed" and res.text == "recovered" and state["n"] == 2
