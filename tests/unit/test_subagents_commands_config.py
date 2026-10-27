import asyncio
import subprocess

from naga_code import checkpoints
from naga_code.commands import REGISTRY, Command, CommandRegistry
from naga_code.config import config_home, load_config
from naga_code.providers.base import ToolCall
from naga_code.subagents.definitions import SubagentDef

SUBMIT = ("submit_result", {"summary": "add() subtracts", "findings": [
    {"title": "wrong operator", "location": "calc.py:2", "severity": "high"}], "files_examined": ["calc.py"],
    "confidence": "high"})


async def test_subagent_completes_bounded_task_with_structured_result(make_app):
    app = await make_app([{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
                          {"text": "", "tool_calls": [SUBMIT]}, "submitted"])
    res = await app.subagents.run("explorer", "What does add do?", app.ctx)
    assert res.ok and res.structured and res.result["findings"][0]["location"] == "calc.py:2"
    assert "Review these results" in res.render()
    assert app.agent.history == []  # isolated context


async def test_read_only_subagent_cannot_write(make_app, project):
    app = await make_app([{"text": "", "tool_calls": [("write_file", {"path": "x.py", "content": "x"})]}, "gave up"])
    res = await app.subagents.run("explorer", "write a file", app.ctx)
    assert not (project / "x.py").exists()
    assert "gave up" in res.result["summary"]
    sent_tools = {t["function"]["name"] for t in app.agent.provider.calls[0]["tools"]}
    assert "write_file" not in sent_tools and "delegate" not in sent_tools and "submit_result" in sent_tools


async def test_subagent_timeout(make_app):
    app = await make_app()
    app.subagents.defs["slow"] = SubagentDef("slow", "slow", "x", timeout=0.5)
    app.agent.provider.delay = 0.3
    app.agent.provider.script = ["a" * 200]
    res = await app.subagents.run("slow", "t", app.ctx)
    assert res.stop_reason == "timeout" and not res.ok


async def test_parallel_delegation_via_tool(make_app):
    app = await make_app([{"text": "", "tool_calls": [SUBMIT]}, "ok", {"text": "", "tool_calls": [SUBMIT]}, "ok"])
    res = await app.registry.execute(ToolCall("delegate", {"tasks": [
        {"agent": "explorer", "task": "a"}, {"agent": "security-reviewer", "task": "b"}]}), app.ctx)
    assert res.ok and res.output.count("## Subagent") == 2


async def test_conflicting_writes_are_detected(make_app, project):
    app = await make_app()
    run = lambda name, **a: app.registry.execute(ToolCall(name, a), app.ctx)  # noqa: E731
    await run("read_file", path="calc.py")
    sub_ctx = app.ctx.derive(agent_name="sub:implementer")
    r1 = app.registry.execute(ToolCall("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"}),
                              app.ctx)
    r2 = app.registry.execute(ToolCall("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a * b"}),
                              sub_ctx)
    a, b = await asyncio.gather(r1, r2)
    assert a.ok != b.ok  # the second writer sees a stale file / missing text instead of clobbering
    assert (project / "calc.py").read_text().count("return") == 1


class FakeUI:
    def __init__(self):
        self.out, self.verbose, self.last_result = [], False, None
        self.console = self
        self.answers = []

    def print(self, *a, **k):
        self.out.append(" ".join(str(x) for x in a))

    def clear(self):
        pass

    def status(self, *a, **k):
        import contextlib
        return contextlib.nullcontext()

    info = warn = error = success = show_diff = lambda self, m: self.out.append(str(m))

    async def confirm(self, q, default=False):
        return self.answers.pop(0) if self.answers else default

    async def run_agent(self, text, mode=None):
        self.out.append(f"AGENT[{mode}]: {text[:40]}")


async def test_slash_commands_dispatch(make_app):
    app = await make_app()
    ui = FakeUI()
    assert await REGISTRY.dispatch(ui, app, "/help")
    assert await REGISTRY.dispatch(ui, app, "/nonexistent")
    assert any("unknown command" in o for o in ui.out)
    assert not await REGISTRY.dispatch(ui, app, "/quit")
    for line in ("/status", "/tools", "/skills", "/agents", "/permissions", "/context", "/git", "/mcp", "/hooks",
                 "/memory add --user prefers tabs", "/memory", "/files *.py", "/search return", "/tasks", "/sessions",
                 "/mode ask", "/config get permissions.mode", "/export", "/checkpoint", "/doctor", "/provider"):
        ui.out.clear()
        assert await REGISTRY.dispatch(ui, app, line), line
        assert not any(o.startswith("/") and ": " in o and "Error" in o for o in ui.out), (line, ui.out)
    assert app.mode == "ask"
    await REGISTRY.dispatch(ui, app, "/review")
    assert any(o.startswith("AGENT[ask]") for o in ui.out)


async def test_project_commands_cannot_override_builtins():
    reg = CommandRegistry()

    async def h(ui, app, args):
        return None
    reg.add(Command("help", "builtin", h))
    reg.add(Command("help", "project", h, protected=False, group="project"))
    assert reg.resolve("help").help == "builtin" and reg.resolve("project:help").help == "project"
    assert reg.warnings


def test_project_config_cannot_loosen_security(project):
    (project / ".naga").mkdir()
    (project / ".naga" / "config.toml").write_text(
        '[permissions]\nmode = "accept-edits"\nallow_commands = ["rm"]\ndeny_commands = ["docker"]\n'
        '[web]\nenabled = true\n[agent]\nmax_iterations = 5\n')
    cfg = load_config(project)
    assert cfg.get("permissions.mode") == "default" and "rm" not in cfg.get("permissions.allow_commands")
    assert "docker" in cfg.get("permissions.deny_commands") and cfg.get("web.enabled") is False
    assert cfg.get("agent.max_iterations") == 5
    assert any("permissions.mode" in w for w in cfg.warnings)


def test_user_config_and_invalid_config(project):
    p = config_home() / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[profiles.coding]\nmodel = "my-model"\n')
    assert load_config(project).profile("coding")["model"] == "my-model"
    p.write_text("not = [valid")
    cfg = load_config(project)
    assert cfg.warnings and cfg.profile("coding")["model"] == "qwen3-coder:latest"


async def test_checkpoint_and_git_preservation(make_app, project):
    (project / "user_notes.txt").write_text("user's uncommitted work\n")
    app = await make_app()
    run = lambda name, **a: app.registry.execute(ToolCall(name, a), app.ctx)  # noqa: E731
    await run("read_file", path="calc.py")
    await run("edit_file", path="calc.py", old_string="a - b", new_string="a + b")
    path = checkpoints.create(app, "before tests")
    rev = path.with_suffix("").with_suffix(".reverse.patch")
    assert "+    return a + b" in path.read_text() and "-    return a + b" in rev.read_text()
    assert "user_notes.txt" not in path.read_text()  # only agent changes are captured
    r = subprocess.run(["git", "apply", "--check", str(rev)], cwd=project, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (project / "user_notes.txt").read_text() == "user's uncommitted work\n"


async def test_git_commit_requires_approval_and_stage(make_app, project):
    from conftest import Approver

    from naga_code.security.permissions import Choice
    approver = Approver(Choice.APPROVE_ONCE, Choice.DENY)
    app = await make_app(approver=approver)
    (project / "calc.py").write_text("changed\n")
    res = await app.registry.execute(ToolCall("git_stage", {"paths": ["calc.py"]}), app.ctx)
    assert res.ok
    res = await app.registry.execute(ToolCall("git_commit", {"message": "x"}), app.ctx)
    assert not res.ok and "denied" in res.output
    log = subprocess.run(["git", "log", "--oneline"], cwd=project, capture_output=True, text=True).stdout
    assert log.count("\n") == 1


class ChoosingUI(FakeUI):
    def __init__(self, picks):
        super().__init__()
        self.picks, self.menus = list(picks), []

    async def choose(self, question, options, default=0):
        self.menus.append((question, options, default))
        return self.picks.pop(0)


async def test_model_menu_switches_and_can_save_default(make_app):
    app = await make_app()
    ui = ChoosingUI(["fake-model", "session"])
    assert await REGISTRY.dispatch(ui, app, "/model")
    (q, opts, _), (q2, _, _) = ui.menus
    assert "Select a model" in q and opts[0][0] == "fake-model" and "Keep fake-model" in q2
    assert app.model_name == "fake-model"
    assert "model" not in load_config(app.project_root).profile(app.profile_name) or \
        load_config(app.project_root).profile(app.profile_name)["model"] != "fake-model"

    ui = ChoosingUI(["fake-model", "default"])
    assert await REGISTRY.dispatch(ui, app, "/model")
    assert load_config(app.project_root).profile(app.profile_name)["model"] == "fake-model"

    ui = ChoosingUI([None])            # Esc: nothing changes
    assert await REGISTRY.dispatch(ui, app, "/model")
    assert len(ui.menus) == 1 and any("unchanged" in o for o in ui.out)

    ui = ChoosingUI([])                # /model list never opens a menu
    assert await REGISTRY.dispatch(ui, app, "/model list") and not ui.menus


async def test_profile_switch_drops_a_previous_model_override(make_app):
    app = await make_app()
    await app.configure_model(model="other-model")
    await app.configure_model(profile="fast")
    assert app.model_name == app.config.profile("fast")["model"]
