"""Process isolation for agent-run commands.

Layers, from always-on to optional:

1. Scrubbed environment: secret-looking variables (``*_KEY``, ``*_TOKEN``, ...)
   are removed before any command runs, so they are not visible to the model
   through command output.
2. Confined working directory inside the workspace, no stdin, own process group
   (so timeouts/cancellation kill the whole tree), core dumps disabled and a
   file-size cap.
3. Optional bubblewrap (``bwrap``): read-only host filesystem, writable
   workspace and /tmp only. Used when ``terminal.sandbox`` is ``bwrap`` or
   ``auto`` and bwrap actually works on this host (Ubuntu 24.04's AppArmor
   ``apparmor_restrict_unprivileged_userns`` blocks it by default).

No layer is a complete security boundary. A container or bwrap profile with
broad mounts, privileged mode, or access to the Docker socket gives commands
the same power as the host user.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .redact import is_secret_env

FILE_SIZE_LIMIT = 4 * 1024 ** 3


@lru_cache(maxsize=1)
def bwrap_status() -> tuple[bool, str]:
    exe = shutil.which("bwrap")
    if not exe:
        return False, "bwrap not installed"
    try:
        r = subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "true"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"bwrap failed: {e}"
    if r.returncode == 0:
        return True, "bwrap available"
    return False, ("bwrap installed but unusable: " + r.stderr.strip()[:200] +
                   " (unprivileged user namespaces are likely restricted by AppArmor)")


def scrub_env(base: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (base if base is not None else os.environ).items() if not is_secret_env(k)}
    env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "TERM": "dumb",
                "NAGA_AGENT": "1", "PYTHONUNBUFFERED": "1", "CI": env.get("CI", "1")})
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("GPG_AGENT_INFO", None)
    if extra:
        env.update(extra)
    return env


def _preexec() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, FILE_SIZE_LIMIT))
    except (ValueError, OSError):
        pass


@dataclass
class Sandbox:
    workspace: Path
    mode: str = "auto"  # auto | none | bwrap
    network: bool = True

    @property
    def active(self) -> str:
        if self.mode == "none":
            return "none"
        ok, _ = bwrap_status()
        if ok:
            return "bwrap"
        if self.mode == "bwrap":
            raise RuntimeError("terminal.sandbox = 'bwrap' but " + bwrap_status()[1])
        return "basic"

    def wrap(self, argv: list[str]) -> list[str]:
        if self.active != "bwrap":
            return argv
        ws = str(self.workspace)
        wrapped = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                   "--bind", ws, ws, "--die-with-parent", "--new-session"]
        if not self.network:
            wrapped.append("--unshare-net")
        return wrapped + ["--"] + argv

    preexec = staticmethod(_preexec)

    def describe(self) -> str:
        try:
            active = self.active
        except RuntimeError as e:
            return str(e)
        if active == "bwrap":
            return "bubblewrap: read-only host FS, writable workspace + private /tmp, scrubbed env"
        if active == "basic":
            return ("basic: scrubbed env, workspace cwd, process-group kill, rlimits "
                    f"(no OS-level FS isolation — {bwrap_status()[1]})")
        return "none: scrubbed env only"
