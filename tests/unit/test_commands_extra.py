import json
import subprocess

from naga_code import background, commands_extra, plugins
from naga_code.commands import REGISTRY
from naga_code.config import config_home, load_config
from naga_code.security.permissions import Choice
from naga_code.ui import style

from conftest import Approver

# Every command from the reference list of a popular coding agent (minus two brand-named ones).
REFERENCE = """add-dir advisor agents artifact-capabilities artifact-diagramming artifacts auto-mode-setup autocompact
autofix-pr background batch branch btw bug cd chrome clear code-review color compact config context copy cost
dataviz debug deep-research design design-login design-sync desktop diff doctor effort exit export fast feedback
fewer-permission-prompts focus fork goal heapdump help hooks ide import init insights install-github-app
install-slack-app keybindings list-agents login logout loop mcp memory mobile model output-style passes permissions
plan plugin plugin-authoring powerup pr-comments privacy-settings radio rate-limit-options recap release-notes
reload-plugins reload-skills remote-control remote-env rename resume review rewind run run-skill-generator sandbox
schedule scroll-speed security-review setup-bedrock setup-vertex simplify skill-doctor skills slides stats status
statusline stickers stop subtask tasks team-onboarding teleport terminal-setup theme tui ultraplan ultrareview
update-config upgrade usage usage-credits verify vim voice web-setup workflow-authoring workflows
bg share reset new settings checkup quit proactive allowed-tools continue routines""".split()


class UI:
    def __init__(self, app=None, picks=(), confirms=()):
        self.out, self.errors, self.verbose, self.last_result = [], [], False, None
        self.console = self
        self.picks, self.confirms = list(picks), list(confirms)
        self.app = app
        self.prefill = ""

    def print(self, *a, **k):
        def text(x):
            x = getattr(x, "renderable", x)      # Panel -> its content
            return getattr(x, "markup", str(x))  # Markdown -> its source
        self.out.append(" ".join(text(x) for x in a))

    def clear(self):
        pass

    def status(self, *a, **k):
        import contextlib
        return contextlib.nullcontext()

    color_system = "truecolor"
    info = success = warn = show_diff = lambda self, m: self.out.append(str(m))

    def error(self, m):
        self.errors.append(str(m))

    async def confirm(self, q, default=False):
        return self.confirms.pop(0) if self.confirms else default

    async def choose(self, q, options, default=0):
        return self.picks.pop(0) if self.picks else options[default][0]

    async def run_agent(self, text, mode=None):
        if mode:
            self.app.set_mode(mode)
        return await self.app.run(text)


async def run(ui, app, line):
    assert await REGISTRY.dispatch(ui, app, line)
    assert not ui.errors, (line, ui.errors)


def test_every_reference_command_exists():
    missing = [n for n in REFERENCE if REGISTRY.resolve(n) is None]
    assert not missing, missing
    visible = REGISTRY.names(include_hidden=False)
    assert "rewind" in visible and "login" not in visible


async def test_unavailable_commands_explain_themselves(make_app):
    app = await make_app()
    ui = UI(app)
    await run(ui, app, "/login")
    assert "not available" in ui.out[-1]


async def test_btw_does_not_touch_history(make_app):
    app = await make_app(["Done.", "It returns a - b."])
    ui = UI(app)
    await app.run("look at calc.py")
    before = list(app.agent.history)
    await run(ui, app, "/btw what does add do?")
    assert app.agent.history == before
    assert any("a - b" in o for o in ui.out)


async def test_rewind_restores_code_and_conversation(make_app, project):
    script = [{"text": "", "tool_calls": [("write_file", {"path": "new.txt", "content": "one\n"})]}, "Created.",
              {"text": "", "tool_calls": [("write_file", {"path": "new2.txt", "content": "two\n"})]}, "Created again."]
    app = await make_app(script, approver=Approver(Choice.APPROVE_ONCE, Choice.APPROVE_ONCE),
                         overrides={"agent": {"require_validation_nudge": False}})
    await app.run("create new.txt")
    await app.run("create new2.txt")
    assert (project / "new2.txt").exists()
    ui = UI(app, picks=["1", "both"])   # newest first: "1" is the second user message
    await run(ui, app, "/rewind")
    assert not (project / "new2.txt").exists() and (project / "new.txt").exists()
    assert [m.content for m in app.agent.history if m.role == "user"] == ["create new.txt"]
    assert ui.prefill == "create new2.txt"


async def test_branch_rename_and_clear(make_app):
    app = await make_app(["Hi."])
    ui = UI(app)
    await app.run("hello")
    original = app.session.id
    await run(ui, app, "/branch experiment")
    assert app.session.id != original and app.session.title == "experiment"
    assert {r["id"] for r in app.sessions.list()} >= {original, app.session.id}
    await run(ui, app, "/rename better name")
    assert app.sessions.load(app.session.id).title == "better name"
    await run(ui, app, "/new")
    assert app.agent.history == []


async def test_add_dir_edits_always_need_approval(make_app, tmp_path):
    extra = tmp_path / "shared"
    extra.mkdir()
    approver = Approver(Choice.APPROVE_ONCE)
    app = await make_app([{"text": "", "tool_calls": [("write_file", {"path": str(extra / "x.txt"), "content": "x"})]},
                          "ok"], approver=approver, overrides={"permissions": {"mode": "accept-edits"}})
    await run(UI(app), app, f"/add-dir {extra}")
    await app.run("write x")
    assert (extra / "x.txt").read_text() == "x"
    assert approver.requests and approver.requests[0].outside_project


async def test_goal_style_effort_autocompact(make_app):
    app = await make_app(["MET: tests pass"])
    ui = UI(app)
    await run(ui, app, "/goal all tests pass")
    await run(ui, app, "/output-style concise")
    prompt = app.system_prompt()
    assert "all tests pass" in prompt and "extremely brief" in prompt
    await commands_extra.after_turn(ui, app, type("R", (), {"stop_reason": "completed"})())
    assert app.goal is None and "goal" not in app.prompt_extras
    await run(ui, app, "/effort low")
    assert app.agent.options.think is False
    await run(ui, app, "/autocompact 10k")
    assert abs(app.agent.context.budget.compact_threshold - 10000 / app.agent.context.budget.usable) < 0.01
    assert load_config(app.project_root).get("ui.output_style") == "concise"


async def test_plugins_add_commands_and_skills(make_app, tmp_path):
    src = tmp_path / "myplug"
    (src / "skills" / "greet").mkdir(parents=True)
    (src / "skills" / "greet" / "SKILL.md").write_text("---\nname: greet\ndescription: Greets the user politely when asked to.\n---\n"
        "When the user asks for a greeting, reply with a short friendly hello that uses their name if known.\n")
    (src / "commands").mkdir()
    (src / "commands" / "hello.md").write_text("# Say hello\nSay hello to $ARGUMENTS\n")
    app = await make_app()
    ui = UI(app)
    await run(ui, app, f"/plugin install {src}")
    assert REGISTRY.resolve("hello") is not None and "greet" in app.skills.skills
    await run(ui, app, "/plugin disable myplug")
    assert REGISTRY.resolve("hello") is None and "greet" not in app.skills.skills
    await run(UI(app, confirms=[True]), app, "/plugin remove myplug")
    assert not plugins.installed()


async def test_import_and_config_pairs(make_app, project):
    (project / "GEMINI.md").write_text("Use tabs.\n")
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"docs": {"command": "docs-server"}}}))
    app = await make_app()
    ui = UI(app)
    await run(ui, app, "/import --yes")
    assert "Use tabs." in (project / "NAGA.md").read_text()
    assert "docs" in json.loads((config_home() / "mcp.json").read_text())["mcpServers"]
    await run(ui, app, "/config agent.max_iterations=12")
    assert load_config(app.project_root).get("agent.max_iterations") == 12


async def test_copy_theme_color_focus(make_app, monkeypatch):
    app = await make_app(["Answer text."])
    await app.run("q")
    copied = []
    monkeypatch.setattr(commands_extra, "_copy_to_clipboard", lambda t: copied.append(t) or "test")
    ui = UI(app)
    await run(ui, app, "/copy")
    assert copied == ["Answer text."]
    await run(ui, app, "/theme light")
    await run(ui, app, "/color green")
    await run(ui, app, "/focus")
    assert style.STATE["syntax"] == "ansi_light" and style.STATE["prompt_color"] == "green" and style.STATE["focus"]
    style.set_theme("dark")
    style.STATE.update(prompt_color="default", focus=False)


async def test_background_jobs_are_tracked_and_stoppable(make_app, monkeypatch):
    import types
    real_popen = subprocess.Popen   # patch only background's view of subprocess, not the shared module
    fake = types.SimpleNamespace(Popen=lambda argv, **kw: real_popen(["sleep", "30"], **kw),
                                 STDOUT=subprocess.STDOUT, DEVNULL=subprocess.DEVNULL)
    monkeypatch.setattr(background, "subprocess", fake)
    app = await make_app(["Hi."])
    await app.run("hello")
    old = app.session.id
    ui = UI(app)
    await run(ui, app, "/background keep going")
    job = background.jobs()[-1]
    assert job.session == old and job.running() and app.session.id != old
    await run(ui, app, "/list-agents")
    assert any(job.id in o for o in ui.out)
    await run(ui, app, f"/stop {job.id}")
    for _ in range(50):
        if not job.running():
            break
        import time
        time.sleep(0.05)
    assert not job.running()


async def test_info_commands_run_cleanly(make_app):
    app = await make_app(["Recap text."])
    ui = UI(app)
    for line in ("/context all", "/cost", "/stats", "/insights", "/heapdump", "/keybindings", "/privacy-settings",
                 "/release-notes", "/skill-doctor", "/reload-skills", "/reload-plugins", "/terminal-setup", "/sandbox keep",
                 "/list-agents", "/goal", "/advisor off", "/fast on", "/fast off", "/mcp reconnect all", "/schedule list",
                 "/fewer-permission-prompts"):
        ui.out.clear()
        await run(ui, app, line)
