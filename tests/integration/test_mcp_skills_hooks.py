import json
import sys
from pathlib import Path

from conftest import Approver

from naga_code.config import config_home
from naga_code.mcp.manager import MCPManager
from naga_code.security.permissions import Choice
from naga_code.skills import SkillLibrary
from naga_code.tools.registry import ToolRegistry

SERVER = str(Path(__file__).parent.parent / "fixtures" / "mcp_echo_server.py")


def write_mcp(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


# ------------------------------------------------------------------- MCP
async def test_mcp_stdio_connect_list_call(project):
    write_mcp(config_home() / "mcp.json", {"echo": {"command": sys.executable, "args": [SERVER], "permission": "read"}})
    reg = ToolRegistry()
    mgr = MCPManager(project, reg)
    st = await mgr.connect("echo")
    try:
        assert st.status == "connected", st.error
        assert st.client.server_info["name"] == "echo-server" and st.client.protocol_version == "2025-06-18"
        assert set(st.tool_names) == {"mcp__echo__echo", "mcp__echo__add"}
        ok, text = await st.client.call_tool("add", {"a": 2, "b": 3})
        assert ok and text == "5"
        assert (await st.client.list_resources())[0]["uri"] == "mem://greeting"
        assert await st.client.ping()
    finally:
        await mgr.shutdown()
    assert mgr.servers["echo"].status == "disconnected" and reg.get("mcp__echo__add") is None


async def test_mcp_tool_through_agent_with_permissions(make_app, isolated_home):
    write_mcp(config_home() / "mcp.json", {"echo": {"command": sys.executable, "args": [SERVER]}})
    approver = Approver(Choice.APPROVE_ONCE)
    app = await make_app([{"text": "", "tool_calls": [("mcp__echo__echo", {"text": "hi from mcp"})]}, "done"],
                         approver=approver)
    st = await app.mcp.connect("echo")
    assert st.status == "connected"
    await app.run("call echo")
    tool_msg = [m for m in app.agent.history if m.role == "tool"][0]
    assert "hi from mcp" in tool_msg.content and "untrusted" in tool_msg.content
    assert approver.requests[0].tool == "mcp:echo"  # default permission = execute -> approval asked


async def test_mcp_failures(project):
    write_mcp(config_home() / "mcp.json", {
        "missing": {"command": "/nonexistent/server"},
        "hang": {"command": sys.executable, "args": [SERVER, "hang"]},
        "crash": {"command": sys.executable, "args": [SERVER, "crash"]},
        "bad": {"command": sys.executable, "args": [SERVER, "badschema"]},
    })
    mgr = MCPManager(project, ToolRegistry(), connect_timeout=1.5)
    try:
        assert (await mgr.connect("missing")).status == "error"
        hang = await mgr.connect("hang")
        assert hang.status == "error" and "Timeout" in hang.error
        assert (await mgr.connect("crash")).status == "error"
        bad = await mgr.connect("bad")
        assert bad.status == "connected" and "mcp__bad__broken" not in bad.tool_names
        assert any("invalid input schema" in e for e in mgr.errors)
    finally:
        await mgr.shutdown()


async def test_project_mcp_servers_need_trust(project):
    write_mcp(project / ".naga" / "mcp.json", {"proj": {"command": sys.executable, "args": [SERVER], "permission": "read",
                                                        "auto_connect": True}})
    mgr = MCPManager(project, ToolRegistry())
    c = mgr.servers["proj"].config
    assert c.permission == "execute" and not c.auto_connect  # repo cannot self-approve
    assert (await mgr.connect("proj")).status == "untrusted"
    mgr.trust("proj")
    try:
        assert (await mgr.connect("proj")).status == "connected"
    finally:
        await mgr.shutdown()


# ---------------------------------------------------------------- skills
def test_builtin_skills_discovered_and_progressive(project):
    lib = SkillLibrary(project)
    assert len(lib.skills) >= 16 and not lib.problems
    index = lib.index_prompt()
    assert "code-review:" in index and "# Code review" not in index  # only descriptions in the index
    rendered = lib.render(lib.get("code-review"), "src/")
    assert "# Code review" in rendered and "references/review-checklist.md" in rendered and "src/" in rendered
    assert "Correctness" in lib.read_file(lib.get("code-review"), "references/review-checklist.md")


def test_malformed_and_overriding_skills(project):
    d = project / ".naga" / "skills"
    (d / "broken").mkdir(parents=True)
    (d / "broken" / "SKILL.md").write_text("no frontmatter here")
    (d / "badyaml").mkdir()
    (d / "badyaml" / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody")
    (d / "wrongname").mkdir()
    (d / "wrongname" / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\n" + "instructions " * 10)
    (d / "bug-fix").mkdir()
    (d / "bug-fix" / "SKILL.md").write_text("---\nname: bug-fix\ndescription: project version\n---\n" + "do it " * 20)
    lib = SkillLibrary(project)
    errors = {p.path.parent.name: p.error for p in lib.problems}
    assert "frontmatter" in errors["broken"] and "YAML" in errors["badyaml"] and "match" in errors["wrongname"]
    assert lib.get("bug-fix").source == "project" and lib.get("bug-fix").description == "project version"
    try:
        lib.read_file(lib.get("bug-fix"), "../../../../etc/passwd")
        raise AssertionError("escaped skill dir")
    except ValueError:
        pass


async def test_agent_loads_skill(make_app):
    app = await make_app([{"text": "", "tool_calls": [("load_skill", {"name": "bug-fix"})]}, "ok"])
    await app.run("fix a bug")
    tool_msg = [m for m in app.agent.history if m.role == "tool"][0]
    assert "# Bug fix" in tool_msg.content and "regression test" in tool_msg.content


# ----------------------------------------------------------------- hooks
async def test_hooks_run_block_and_require_trust(make_app, project, isolated_home):
    marker = project / "hook_ran.txt"
    cfg = config_home() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'[[hooks]]\nevent = "after_file_modification"\ncommand = "echo {{path}} >> {marker}"\n'
                   f'match = "*.py"\n\n[[hooks]]\nevent = "before_tool"\ncommand = "echo nope >&2; exit 2"\n'
                   f'match = "run_command"\n')
    (project / ".naga").mkdir(exist_ok=True)
    (project / ".naga" / "config.toml").write_text('[[hooks]]\nevent = "session_start"\ncommand = "touch PWNED"\n')
    script = [{"text": "", "tool_calls": [("read_file", {"path": "calc.py"})]},
              {"text": "", "tool_calls": [("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})]},
              {"text": "", "tool_calls": [("run_command", {"command": "ls"})]}, "done", "done"]
    app = await make_app(script)
    assert not (project / "PWNED").exists()  # untrusted project hook did not run
    await app.run("go")
    assert marker.read_text().strip() == "calc.py"
    blocked = [m for m in app.agent.history if m.role == "tool" and m.name == "run_command"][0]
    assert "blocked by hook" in blocked.content and "nope" in blocked.content
    app.hooks.trust_project()
    await app.hooks.run("session_start", {})
    assert (project / "PWNED").exists()
