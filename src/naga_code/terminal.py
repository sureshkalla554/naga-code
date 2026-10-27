"""Terminal engine: bounded, cancellable, sandbox-wrapped subprocess execution."""

from __future__ import annotations

import asyncio
import itertools
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .security.sandbox import Sandbox, scrub_env


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def render(self, max_chars: int | None = None) -> str:
        status = ("TIMED OUT" if self.timed_out else "CANCELLED" if self.cancelled
                  else f"exit code {self.exit_code}")
        parts = [f"$ {self.command}", f"[{status}; {self.duration_s:.2f}s{'; output truncated' if self.truncated else ''}]"]
        if self.stdout:
            parts.append("--- stdout ---\n" + self.stdout.rstrip())
        if self.stderr:
            parts.append("--- stderr ---\n" + self.stderr.rstrip())
        if not self.stdout and not self.stderr:
            parts.append("(no output)")
        text = "\n".join(parts)
        if max_chars and len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + f"\n... [{len(text) - max_chars} chars omitted] ...\n" + text[-half:]
        return text


class _Capture:
    """Keeps the head and tail of a stream within a byte budget."""

    def __init__(self, limit: int):
        self.limit = limit
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_size = 0
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self.limit // 2 - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self.tail.append(chunk)
        self.tail_size += len(chunk)
        while self.tail_size > self.limit // 2 and self.tail:
            first = self.tail[0]
            excess = self.tail_size - self.limit // 2
            if len(first) <= excess:
                self.tail.popleft()
                self.tail_size -= len(first)
                self.dropped += len(first)
            else:
                self.tail[0] = first[excess:]
                self.tail_size -= excess
                self.dropped += excess

    def text(self) -> str:
        mid = f"\n... [{self.dropped} bytes omitted] ...\n".encode() if self.dropped else b""
        return (bytes(self.head) + mid + b"".join(self.tail)).decode("utf-8", errors="replace")


@dataclass
class BackgroundProcess:
    id: int
    command: str
    cwd: str
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.time)
    buffer: deque[str] = field(default_factory=lambda: deque(maxlen=4000))
    read_pos: int = 0
    total_lines: int = 0
    reader: asyncio.Task | None = None

    @property
    def status(self) -> str:
        rc = self.proc.returncode
        return "running" if rc is None else f"exited ({rc})"


class TerminalEngine:
    def __init__(self, workspace_root: Path, sandbox: Sandbox | None = None, shell: str = "/bin/bash",
                 default_timeout: float = 120, max_timeout: float = 1800, max_output_chars: int = 200_000):
        self.root = workspace_root
        self.sandbox = sandbox or Sandbox(workspace_root, "none")
        self.shell = shell
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars
        self.background: dict[int, BackgroundProcess] = {}
        self._ids = itertools.count(1)

    def _argv(self, command: str | list[str]) -> tuple[list[str], str]:
        if isinstance(command, list):
            return command, " ".join(command)
        return [self.shell, "-c", command], command

    async def _spawn(self, argv: list[str], cwd: Path, env: dict[str, str] | None) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self.sandbox.wrap(argv), cwd=str(cwd), env=scrub_env(extra=env), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            preexec_fn=self.sandbox.preexec)

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def run(self, command: str | list[str], cwd: Path | None = None, timeout: float | None = None,
                  env: dict[str, str] | None = None, max_output: int | None = None) -> CommandResult:
        argv, display = self._argv(command)
        timeout = min(timeout or self.default_timeout, self.max_timeout)
        limit = max_output or self.max_output_chars
        out, err = _Capture(limit), _Capture(limit // 2)
        t0 = time.monotonic()
        proc = await self._spawn(argv, cwd or self.root, env)

        async def pump(stream: asyncio.StreamReader, cap: _Capture) -> None:
            while chunk := await stream.read(65536):
                cap.add(chunk)

        pumps = asyncio.gather(pump(proc.stdout, out), pump(proc.stderr, err))  # type: ignore[arg-type]
        timed_out = cancelled = False
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill(proc)
        except asyncio.CancelledError:
            cancelled = True
            await self._kill(proc)
        try:
            await asyncio.wait_for(pumps, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pumps.cancel()
        result = CommandResult(display, proc.returncode, out.text(), err.text(), time.monotonic() - t0,
                               timed_out, cancelled, bool(out.dropped or err.dropped))
        if cancelled:
            raise asyncio.CancelledError()
        return result

    # ---- background processes -------------------------------------------
    async def start(self, command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> BackgroundProcess:
        argv, display = self._argv(command)
        proc = await self._spawn(argv, cwd or self.root, env)
        bp = BackgroundProcess(next(self._ids), display, str(cwd or self.root), proc)

        async def reader(stream: asyncio.StreamReader, tag: str) -> None:
            while line := await stream.readline():
                bp.buffer.append(tag + line.decode("utf-8", errors="replace").rstrip("\n"))
                bp.total_lines += 1

        bp.reader = asyncio.ensure_future(asyncio.gather(reader(proc.stdout, ""), reader(proc.stderr, "[err] ")))  # type: ignore[arg-type]
        self.background[bp.id] = bp
        return bp

    def poll(self, pid: int, max_lines: int = 200) -> str:
        bp = self.background.get(pid)
        if not bp:
            raise KeyError(f"no background process #{pid}")
        dropped = bp.total_lines - len(bp.buffer)
        start = max(bp.read_pos - dropped, 0)
        lines = list(bp.buffer)[start:]
        new = lines[-max_lines:]
        bp.read_pos = bp.total_lines
        skipped = len(lines) - len(new)
        head = f"process #{bp.id} [{bp.status}] $ {bp.command}"
        if skipped:
            head += f"\n... [{skipped} earlier new lines omitted]"
        return head + ("\n" + "\n".join(new) if new else "\n(no new output)")

    async def stop(self, pid: int) -> str:
        bp = self.background.get(pid)
        if not bp:
            raise KeyError(f"no background process #{pid}")
        await self._kill(bp.proc)
        if bp.reader:
            try:
                await asyncio.wait_for(bp.reader, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                bp.reader.cancel()
        return f"process #{pid} stopped ({bp.status})"

    async def shutdown(self) -> None:
        for pid in list(self.background):
            await self.stop(pid)
