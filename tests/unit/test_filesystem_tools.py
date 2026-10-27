from naga_code.providers.base import ToolCall
from naga_code.security.permissions import Choice
from naga_code.tools.patching import PatchError, apply_hunks, parse_patch, unified_diff

from conftest import Approver


async def run(app, tool, **args):
    return await app.registry.execute(ToolCall(name=tool, arguments=args), app.ctx)


async def test_invalid_arguments_are_not_executed(make_app, project):
    app = await make_app()
    res = await run(app, "edit_file", path="calc.py")  # missing old/new strings
    assert not res.ok and "invalid arguments" in res.output and "not executed" in res.output
    res = await run(app, "nope")
    assert not res.ok and "unknown tool" in res.output
    bad = ToolCall(name="read_file", arguments={}, parse_error="invalid JSON")
    res = await app.registry.execute(bad, app.ctx)
    assert not res.ok and "NOT executed" in res.output


async def test_edit_requires_read_and_detects_user_changes(make_app, project):
    app = await make_app()
    res = await run(app, "edit_file", path="calc.py", old_string="a - b", new_string="a + b")
    assert not res.ok and "read calc.py before" in res.output
    await run(app, "read_file", path="calc.py")
    (project / "calc.py").write_text("def add(a, b):\n    return a - b  # user edit\n")
    res = await run(app, "edit_file", path="calc.py", old_string="a - b", new_string="a + b")
    assert not res.ok and "changed on disk" in res.output
    assert "user edit" in (project / "calc.py").read_text()


async def test_edit_uniqueness_and_hints(make_app, project):
    (project / "dup.py").write_text("x = 1\nx = 1\n")
    app = await make_app()
    await run(app, "read_file", path="dup.py")
    res = await run(app, "edit_file", path="dup.py", old_string="x = 1", new_string="x = 2")
    assert not res.ok and "occurs 2 times" in res.output
    res = await run(app, "edit_file", path="dup.py", old_string="x = 1", new_string="x = 2", replace_all=True)
    assert res.ok and (project / "dup.py").read_text() == "x = 2\nx = 2\n"
    res = await run(app, "edit_file", path="dup.py", old_string="  x = 2", new_string="y")
    assert not res.ok and "not found" in res.output


async def test_edit_preserves_crlf_and_records_diff(make_app, project):
    (project / "win.txt").write_bytes(b"line one\r\nline two\r\n")
    app = await make_app()
    await run(app, "read_file", path="win.txt")
    res = await run(app, "edit_file", path="win.txt", old_string="line two", new_string="line 2")
    assert res.ok and res.diff and "+line 2" in res.diff
    assert (project / "win.txt").read_bytes() == b"line one\r\nline 2\r\n"


async def test_binary_files_refused(make_app, project):
    (project / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    app = await make_app()
    res = await run(app, "read_file", path="blob.bin")
    assert not res.ok and "binary" in res.output


async def test_write_file_does_not_overwrite_silently(make_app, project):
    app = await make_app()
    res = await run(app, "write_file", path="calc.py", content="x")
    assert not res.ok and "already exists" in res.output
    res = await run(app, "write_file", path="new/mod.py", content="print(1)\n")
    assert res.ok and (project / "new/mod.py").read_text() == "print(1)\n"


async def test_denied_edit_leaves_file_untouched(make_app, project):
    app = await make_app(approver=Approver(Choice.DENY))
    await run(app, "read_file", path="calc.py")
    res = await run(app, "edit_file", path="calc.py", old_string="a - b", new_string="a + b")
    assert not res.ok and "permission denied" in res.output
    assert "a - b" in (project / "calc.py").read_text()


async def test_apply_patch_is_atomic_across_files(make_app, project):
    (project / "b.py").write_text("one\ntwo\n")
    app = await make_app()
    await run(app, "read_file", path="calc.py")
    await run(app, "read_file", path="b.py")
    good = unified_diff("def add(a, b):\n    return a - b\n", "def add(a, b):\n    return a + b\n", "calc.py")
    bad = "--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n-nonexistent\n+x\n two\n"
    res = await run(app, "apply_patch", patch=good + bad)
    assert not res.ok and "does not match" in res.output
    assert "a - b" in (project / "calc.py").read_text()  # first file NOT written
    res = await run(app, "apply_patch", patch=good + "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n")
    assert res.ok, res.output
    assert "a + b" in (project / "calc.py").read_text()
    assert (project / "created.txt").read_text() == "hello\nworld\n"


def test_patch_offset_and_whitespace_tolerance():
    text = "".join(f"line {i}\n" for i in range(1, 21))
    patch = "--- a/f\n+++ b/f\n@@ -3,3 +3,3 @@\n line 10\n-line 11\n+LINE 11\n line 12\n"
    out = apply_hunks(text, parse_patch(patch)[0].hunks)
    assert "LINE 11\n" in out and "line 11\n" not in out
    try:
        apply_hunks(text, parse_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-missing\n+x\n")[0].hunks)
        raise AssertionError("expected PatchError")
    except PatchError as e:
        assert "Re-read the file" in str(e)


async def test_undo_restores_only_unmodified_files(make_app, project):
    app = await make_app()
    await run(app, "read_file", path="calc.py")
    await run(app, "edit_file", path="calc.py", old_string="a - b", new_string="a + b")
    ok, msg = app.workspace.undo_last()
    assert ok and "a - b" in (project / "calc.py").read_text()
    await run(app, "read_file", path="calc.py")
    await run(app, "edit_file", path="calc.py", old_string="a - b", new_string="a * b")
    (project / "calc.py").write_text((project / "calc.py").read_text() + "# user tweak\n")
    ok, msg = app.workspace.undo_last()
    assert not ok and "reverse patch" in msg
    assert "# user tweak" in (project / "calc.py").read_text()


async def test_search_and_symbols(make_app, project):
    app = await make_app()
    res = await run(app, "search_code", pattern="return a")
    assert res.ok and "calc.py:2" in res.output
    res = await run(app, "find_symbol", name="add")
    assert "calc.py:1" in res.output
    res = await run(app, "find_references", name="add")
    assert "test_calc.py" in res.output
    res = await run(app, "find_files", pattern="*.py")
    assert "calc.py" in res.output
    (project / "ignored_dir").mkdir()
    (project / "ignored_dir" / "x.py").write_text("return a")
    res = await run(app, "search_code", pattern="return a")
    assert "ignored_dir" not in res.output


async def test_repo_summary_and_detect_project(make_app):
    app = await make_app()
    res = await run(app, "repo_summary")
    assert "Git: main" in res.output and "calc.py" in res.output
    res = await run(app, "detect_project")
    assert "pytest" in res.output


def test_detect_project_without_manifest(tmp_path):
    from naga_code.tools.testing import _python_test_command, detect_project
    (tmp_path / "test_x.py").write_text("def test_a():\n    pass\n")
    _python_test_command.cache_clear()
    pp = detect_project(tmp_path)
    assert pp.kinds == ["python"] and pp.test and "inferred" in pp.notes[0]


async def test_delete_file_is_recorded_and_undoable(make_app, project):
    app = await make_app()
    await run(app, "write_file", path="debug_tmp.py", content="print(1)\n")
    res = await run(app, "delete_file", path="debug_tmp.py")
    assert res.ok and not (project / "debug_tmp.py").exists()
    ok, _ = app.workspace.undo_last()
    assert ok and (project / "debug_tmp.py").exists()
    res = await run(app, "delete_file", path="ignored_dir_does_not_exist")
    assert not res.ok
    res = await run(app, "delete_file", path="../outside.txt")
    assert not res.ok and "outside the project" in res.output
