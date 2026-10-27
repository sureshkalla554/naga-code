import asyncio
import time

import pytest

from naga_code.terminal import TerminalEngine


@pytest.fixture
def term(tmp_path):
    return TerminalEngine(tmp_path, default_timeout=10, max_output_chars=2000)


async def test_captures_exit_code_streams_and_duration(term):
    res = await term.run("echo out; echo err >&2; exit 3")
    assert res.exit_code == 3 and "out" in res.stdout and "err" in res.stderr and not res.ok
    assert res.duration_s >= 0 and "[exit code 3" in res.render()


async def test_argument_array_has_no_shell_interpretation(term, tmp_path):
    res = await term.run(["echo", "$(touch pwned)", ";", "ls"])
    assert res.ok and "$(touch pwned)" in res.stdout
    assert not (tmp_path / "pwned").exists()


async def test_timeout_kills_process_group(term, tmp_path):
    t0 = time.monotonic()
    res = await term.run("sleep 30 & sleep 30; echo never", timeout=1)
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 6
    assert "never" not in res.stdout


async def test_output_is_bounded_head_and_tail(term):
    res = await term.run("seq 1 100000")
    assert res.truncated and "omitted" in res.stdout
    assert res.stdout.startswith("1\n") and res.stdout.rstrip().endswith("100000")
    assert len(res.stdout) < 3000


async def test_cancellation_stops_command(term):
    task = asyncio.ensure_future(term.run("sleep 30"))
    await asyncio.sleep(0.3)
    task.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 5


async def test_secrets_not_visible_to_commands(term, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "abc123")
    monkeypatch.setenv("HARMLESS_SETTING", "ok")
    res = await term.run('echo "[$SUPER_SECRET_TOKEN]"; printenv HARMLESS_SETTING NAGA_AGENT; env | grep -c abc123')
    assert res.stdout.splitlines() == ["[]", "ok", "1", "0"]


async def test_stdin_is_closed_for_noninteractive(term):
    res = await term.run("read x; echo got:$x", timeout=5)
    assert res.ok and "got:" in res.stdout and not res.timed_out


async def test_background_processes(term):
    bp = await term.start("for i in 1 2 3; do echo tick$i; sleep 0.1; done; sleep 30")
    await asyncio.sleep(0.6)
    out = term.poll(bp.id)
    assert "tick1" in out and "tick3" in out and "running" in out
    assert "no new output" in term.poll(bp.id)
    msg = await term.stop(bp.id)
    assert "stopped" in msg and bp.proc.returncode is not None
    with pytest.raises(KeyError):
        term.poll(999)
