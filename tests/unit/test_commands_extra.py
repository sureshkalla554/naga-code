import json
import os
import subprocess
import time
from pathlib import Path

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
    fake = types.SimpleNamespace(Popen=lambda argv, **kw: real_popen(["sleep", "30"], **kw), run=subprocess.run,
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



async def test_background_job_in_its_own_worktree(make_app, project, monkeypatch):
    import types
    real_popen = subprocess.Popen
    launched = {}

    def fake_popen(argv, **kw):   # stand-in for the headless agent: remember where it was told to work
        launched["argv"], launched["cwd"] = argv, kw["cwd"]
        return real_popen(["true"], **kw)
    monkeypatch.setattr(background, "subprocess", types.SimpleNamespace(
        Popen=fake_popen, run=subprocess.run, STDOUT=subprocess.STDOUT, DEVNULL=subprocess.DEVNULL))
    (project / "calc.py").write_text("def add(a, b):\n    return a - b  # wip\n")   # uncommitted user change
    (project / "notes.txt").write_text("untracked\n")
    app = await make_app(["Hi."])
    await app.run("hello")
    ui = UI(app, confirms=[True, True])
    await run(ui, app, "/background fix the add bug")
    job = background.jobs()[-1]
    wt = Path(job.worktree)
    assert wt.is_dir() and job.branch.startswith("naga/") and str(wt) in launched["argv"]
    assert "--allow-edits" in launched["argv"] and launched["cwd"] == wt
    assert "# wip" in (wt / "calc.py").read_text() and (wt / "notes.txt").exists()   # carried over
    # the "job" edits files in its worktree; the project is untouched
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (wt / "new_test.py").write_text("def test_x():\n    pass\n")
    assert "# wip" in (project / "calc.py").read_text()
    for _ in range(50):
        if not job.running():
            break
        time.sleep(0.05)
    ui.out.clear()
    await run(ui, app, f"/list-agents {job.id}")
    assert any("calc.py" in o and "new_test.py" in o for o in ui.out)
    await run(ui, app, f"/job-apply {job.id}")
    assert "a + b" in (project / "calc.py").read_text() and (project / "new_test.py").exists()
    await run(ui, app, f"/job-discard {job.id}")
    assert not wt.exists()
    branches = subprocess.run(["git", "branch"], cwd=project, capture_output=True, text=True).stdout
    assert job.branch not in branches


async def test_rewind_after_restart_restores_files(make_app, project):
    script = [{"text": "", "tool_calls": [("write_file", {"path": "a.txt", "content": "one\n"})]}, "Created.",
              {"text": "", "tool_calls": [("write_file", {"path": "b.txt", "content": "two\n"})]}, "Created b."]
    app = await make_app(script, approver=Approver(Choice.APPROVE_ONCE, Choice.APPROVE_ONCE),
                         overrides={"agent": {"require_validation_nudge": False}})
    await app.run("create a.txt")
    await app.run("create b.txt")
    sid = app.session.id
    await app.shutdown()                       # Naga is closed...
    app2 = await make_app()                    # ...and started again
    app2.resume(sid)
    assert [c.summary() for c in app2.workspace.changes] == ["created a.txt", "created b.txt"]
    ui = UI(app2, picks=["1", "both"])          # rewind to before "create b.txt"
    await run(ui, app2, "/rewind")
    assert not (project / "b.txt").exists() and (project / "a.txt").read_text() == "one\n"
    ui = UI(app2, picks=["0", "code"])          # and to before "create a.txt"
    await run(ui, app2, "/rewind")
    assert not (project / "a.txt").exists()


def test_correction_hint_on_repeated_rule():
    from naga_code.commands_extra import correction_hint
    hist = ["fix the login bug", "always use pnpm instead of npm in this repo"]
    assert "/memory add" in correction_hint(hist, "Use pnpm, not npm! Always pnpm for this repo")
    assert correction_hint(hist, "now add a logout button") is None
    assert correction_hint(["never commit directly"], "always run the tests first") is None


def _fake_gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1 $2" in
  "issue view") echo '{{"title": "Add multiply", "body": "Please add multiply(a, b) to calc.py", "url": "https://github.com/me/r/issues/7", "comments": [], "labels": []}}' ;;
  "pr create") echo "https://github.com/me/r/pull/8" ;;
esac
""")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


async def test_issue_and_pr_workflow(make_app, project, tmp_path, monkeypatch):
    log = _fake_gh(tmp_path, monkeypatch)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=project, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=project, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=project, check=True)
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]}, "Looked at it.",
              "Add multiply()\n\nAdds `multiply(a, b)` to calc.py, as requested in #7."]
    app = await make_app(script, approver=Approver(Choice.APPROVE_ONCE))
    ui = UI(app, confirms=[True, True])
    await run(ui, app, "/issue 7")
    assert subprocess.run(["git", "branch", "--show-current"], cwd=project, capture_output=True,
                          text=True).stdout.strip() == "naga/issue-7"
    first = [m for m in app.agent.history if m.role == "user"][0].content
    assert "Implement GitHub issue #7: Add multiply" in first and "data written by other people" in first
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef multiply(a, b):\n    return a * b\n")
    subprocess.run(["git", "commit", "-qam", "Add multiply"], cwd=project, check=True)
    await run(ui, app, "/pr")
    calls = log.read_text()
    assert "issue view 7" in calls and "pr create --base main --head naga/issue-7 --title Add multiply()" in calls
    assert subprocess.run(["git", "branch", "-r"], cwd=project, capture_output=True, text=True).stdout.count("naga/issue-7")
    assert any("pull/8" in o for o in ui.out)
