import os

import pytest

from naga_code.providers.base import ToolCall
from naga_code.security.audit import AuditLog
from naga_code.security.redact import contains_secret, is_secret_env, redact
from naga_code.security.sandbox import scrub_env
from naga_code.workspace import PathError, Workspace


def test_path_traversal_and_absolute_escape(project):
    ws = Workspace(project)
    for bad in ("../outside.txt", "/etc/passwd", "sub/../../x", "~/.bashrc"):
        with pytest.raises(PathError):
            ws.resolve(bad)
    assert ws.resolve("calc.py") == project / "calc.py"


def test_symlink_pointing_outside_is_rejected(project, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    os.symlink(secret, project / "link.txt")
    with pytest.raises(PathError):
        Workspace(project).resolve("link.txt")


def test_git_dir_is_not_writable(project):
    with pytest.raises(PathError):
        Workspace(project).resolve(".git/config", write=True)


def test_extra_read_dirs_are_read_only(project, tmp_path):
    other = tmp_path / "docs"
    other.mkdir()
    ws = Workspace(project, extra_read_dirs=[str(other)])
    assert ws.resolve(str(other / "a.md")) == other / "a.md"
    with pytest.raises(PathError):
        ws.resolve(str(other / "a.md"), write=True)


async def test_tools_refuse_paths_outside(make_app):
    app = await make_app()
    for name, args in (("read_file", {"path": "../../etc/passwd"}), ("write_file", {"path": "/tmp/x", "content": "x"}),
                       ("list_directory", {"path": "/"})):
        res = await app.registry.execute(ToolCall(name=name, arguments=args), app.ctx)
        assert not res.ok and "outside the project" in res.output


def test_redaction_patterns():
    s = "token=ghp_abcdefghijklmnopqrstuvwxyz0123 key sk-proj-ABCDEFGHIJKLMNOPQRSTUV password: hunter22 " \
        "postgres://user:pa55word@db/x AKIAABCDEFGHIJKLMNOP"
    out = redact(s)
    for secret in ("ghp_abc", "sk-proj-ABC", "hunter22", "pa55word", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out
    assert contains_secret(s) and not contains_secret("just a normal sentence")


def test_secret_env_vars_are_scrubbed():
    env = scrub_env({"PATH": "/bin", "OPENAI_API_KEY": "x", "GITHUB_TOKEN": "y", "AWS_SECRET_ACCESS_KEY": "z",
                     "DB_PASSWORD": "p", "HOME": "/h"})
    assert "PATH" in env and "HOME" in env
    for k in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD"):
        assert k not in env
    assert is_secret_env("MY_SESSION_COOKIE") and not is_secret_env("TOKENIZERS_PARALLELISM")


def test_audit_log_redacts(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record("tool", arguments={"command": "curl -H 'Authorization: x' https://u:secretpw@h"},
               output="api_key=abcd1234efgh")
    text = (tmp_path / "a.jsonl").read_text()
    assert "secretpw" not in text and "abcd1234efgh" not in text and "[REDACTED]" in text


def test_project_root_never_climbs_into_home(tmp_path, monkeypatch):
    from naga_code.workspace import find_project_root
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    (home / "Desktop" / "repo" / ".git").mkdir(parents=True)
    (home / "Desktop" / "repo" / "src").mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert find_project_root(home / "Desktop") == home / "Desktop"
    assert find_project_root(home / "Desktop" / "repo" / "src") == home / "Desktop" / "repo"
    assert find_project_root(home) == home
