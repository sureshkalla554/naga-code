"""Static risk classification of shell commands.

This is *one* layer of defense used to decide how loudly to ask for approval.
It is not a sandbox: commands that are not provably read-only always require
approval, and execution additionally runs with a scrubbed environment, a
confined working directory and (where available) OS-level isolation.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class Risk(IntEnum):
    READ = 0        # provably read-only, no side effects
    WRITE = 1       # modifies files inside the workspace (or unknown local effects)
    EXECUTE = 2     # runs arbitrary programs / code
    DANGEROUS = 3   # elevated, destructive, credential access, system changes
    BLOCKED = 4     # catastrophic; refused outright


@dataclass
class CommandAssessment:
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    network: bool = False
    outside_project: bool = False
    elevated: bool = False

    def describe(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no specific risk detected"


READ_ONLY_PROGRAMS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "rg", "grep", "egrep", "fgrep", "which", "file", "stat", "tree",
    "du", "df", "uname", "whoami", "id", "date", "echo", "printf", "true", "false", "basename", "dirname",
    "realpath", "readlink", "sort", "uniq", "cut", "tr", "diff", "cmp", "less", "more", "nproc", "free",
    "env", "printenv", "type", "hostname", "uptime", "lscpu", "nvidia-smi", "jq", "md5sum", "sha256sum",
}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog",
                 "grep", "remote", "config", "branch", "tag", "reflog", "cat-file", "merge-base"}
VERSION_FLAGS = {"--version", "-V", "version", "--help", "-h"}

ELEVATION = {"sudo", "su", "doas", "pkexec", "runuser"}
SYSTEM_CHANGING = {"apt", "apt-get", "dpkg", "snap", "dnf", "yum", "pacman", "systemctl", "service",
                   "shutdown", "reboot", "poweroff", "halt", "init", "mount", "umount", "modprobe", "insmod",
                   "rmmod", "useradd", "userdel", "usermod", "passwd", "chpasswd", "crontab", "iptables",
                   "ufw", "swapoff", "swapon", "sysctl", "update-grub", "grub-install", "fdisk", "parted"}
NETWORK_PROGRAMS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "ftp", "ping",
                    "dig", "nslookup", "http", "aria2c"}
NETWORK_SUBCOMMANDS = {
    "git": {"clone", "fetch", "pull", "push", "ls-remote", "submodule"},
    "pip": {"install", "download"}, "pip3": {"install", "download"},
    "uv": {"add", "sync", "pip", "lock", "tool", "run"}, "npm": {"install", "i", "ci", "publish", "update", "add"},
    "pnpm": {"install", "add", "update"}, "yarn": {"install", "add", "upgrade"}, "cargo": {"install", "fetch", "update"},
    "go": {"get", "install", "mod"}, "docker": {"pull", "push", "login", "build", "run"},
    "ollama": {"pull", "push"}, "huggingface-cli": {"download", "upload", "login"},
}
CREDENTIAL_PATHS = re.compile(r"(~|\$HOME|/home/[^/\s]+|/root)/\.(ssh|gnupg|aws|kube|docker|netrc|git-credentials|"
                              r"config/gh|password-store)|/etc/(shadow|sudoers|gshadow)|id_rsa|id_ed25519")
SHELL_SPLIT = re.compile(r"\|\||&&|;|\||\n|(?<![<>])&(?![>&])")
SUBST = re.compile(r"\$\(|`|<\(|>\(")
REDIRECT_WRITE = re.compile(r"(?<![0-9&])>{1,2}(?!&)|\d>{1,2}(?!&)|&>")


def _blocked(argv: list[str], raw: str) -> str | None:
    prog = os.path.basename(argv[0]) if argv else ""
    if re.search(r":\(\)\s*\{\s*:\|:&\s*\};:", raw.replace(" ", "")) or ":(){" in raw.replace(" ", ""):
        return "fork bomb"
    if prog == "rm":
        flags = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
        long = {a for a in argv[1:] if a.startswith("--")}
        recursive = "r" in flags or "R" in flags or "--recursive" in long
        targets = [a for a in argv[1:] if not a.startswith("-")]
        roots = {"/", "/*", "~", "~/", "$HOME", "${HOME}", "/home", "/usr", "/etc", "/var", "/boot", "/bin", "/lib",
                 ".", "./", "..", "*", "/root"}
        if recursive and any(t.rstrip("/") in {r.rstrip("/") for r in roots} or t in roots for t in targets):
            return f"recursive delete of a root-level or broad target ({' '.join(targets)})"
        if "--no-preserve-root" in long:
            return "rm --no-preserve-root"
    if prog.startswith("mkfs") or prog in {"wipefs"}:
        return "filesystem formatting"
    if prog == "dd" and any(a.startswith("of=/dev/") for a in argv):
        return "dd writing to a block device"
    if re.search(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", raw):
        return "redirect into a block device"
    if prog in {"chmod", "chown", "chgrp"} and any(a in {"-R", "--recursive"} for a in argv) and \
            any(a in {"/", "/*", "~", "/home", "/usr", "/etc"} for a in argv):
        return "recursive permission change on a system path"
    return None


def _could_be_blocked(tok: str) -> bool:
    prog = os.path.basename(tok)
    return prog in {"rm", "dd", "chmod", "chown", "chgrp", "wipefs"} or prog.startswith("mkfs")


def _segments(cmd: str) -> list[str]:
    return [s.strip() for s in SHELL_SPLIT.split(cmd) if s.strip()]


def _outside(argv: list[str], root: Path | None) -> bool:
    if root is None:
        return False
    for a in argv[1:]:
        val = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if val.startswith(("~", "$HOME", "${HOME}")):
            return True
        if val.startswith("/") and not val.startswith("/dev/null"):
            try:
                p = Path(val).resolve()
            except (OSError, RuntimeError):
                return True
            if not (p == root or root in p.parents) and not str(p).startswith(("/tmp", "/proc", "/usr/bin")):
                return True
        if ".." in Path(val).parts:
            return True
    return False


def assess_command(command: str | list[str], workspace_root: Path | None = None,
                   allow_prefixes: list[str] | None = None,
                   deny_prefixes: list[str] | None = None) -> CommandAssessment:
    raw = command if isinstance(command, str) else shlex.join(command)
    a = CommandAssessment(risk=Risk.READ)
    for d in deny_prefixes or []:
        if raw.strip().startswith(d):
            return CommandAssessment(Risk.BLOCKED, [f"matches deny rule '{d}'"])
    if SUBST.search(raw):
        a.risk = max(a.risk, Risk.EXECUTE)
        a.reasons.append("uses command/process substitution")
    if REDIRECT_WRITE.search(raw.replace(">/dev/null", "").replace("2>&1", "")):
        a.risk = max(a.risk, Risk.WRITE)
        a.reasons.append("redirects output to a file")
    if CREDENTIAL_PATHS.search(raw):
        a.risk = max(a.risk, Risk.DANGEROUS)
        a.reasons.append("touches credential or secret files")
    if re.search(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", raw):
        a.risk = max(a.risk, Risk.DANGEROUS)
        a.reasons.append("pipes downloaded content into a shell")
    for seg in _segments(raw) if isinstance(command, str) else [raw]:
        try:
            argv = shlex.split(seg)
        except ValueError:
            a.risk = max(a.risk, Risk.EXECUTE)
            a.reasons.append("could not parse shell syntax")
            continue
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):  # VAR=x prefix
            argv = argv[1:]
        if not argv:
            continue
        # Check every position so wrappers (sudo, timeout, xargs, env, nice ...) cannot hide a blocked command.
        for i in range(len(argv)):
            if i and not _could_be_blocked(argv[i]):
                continue
            why = _blocked(argv[i:], seg)
            if why:
                return CommandAssessment(Risk.BLOCKED, [why])
        prog = os.path.basename(argv[0])
        sub = argv[1] if len(argv) > 1 else ""
        if _outside(argv, workspace_root):
            a.outside_project = True
        if prog in ELEVATION:
            a.elevated = True
            a.risk = max(a.risk, Risk.DANGEROUS)
            a.reasons.append(f"requires elevated privileges ({prog})")
            continue
        if prog in SYSTEM_CHANGING:
            a.risk = max(a.risk, Risk.DANGEROUS)
            a.reasons.append(f"changes system state ({prog})")
        if prog in NETWORK_PROGRAMS or sub in NETWORK_SUBCOMMANDS.get(prog, set()):
            a.network = True
        if prog == "git":
            gargs = argv[1:]
            if sub == "reset" and "--hard" in gargs:
                a.risk = max(a.risk, Risk.DANGEROUS)
                a.reasons.append("git reset --hard discards work")
            elif sub == "clean" and any(x.startswith("-") and "f" in x for x in gargs):
                a.risk = max(a.risk, Risk.DANGEROUS)
                a.reasons.append("git clean deletes untracked files")
            elif sub == "push" and any(x in {"-f", "--force", "--force-with-lease", "--mirror", "--delete"}
                                       or x.startswith("+") for x in gargs):
                a.risk = max(a.risk, Risk.DANGEROUS)
                a.reasons.append("force/destructive push")
            elif sub in {"checkout", "restore"} and ("--" in gargs or "." in gargs) or \
                    sub == "branch" and any(x in {"-D", "--delete", "-d"} for x in gargs) or \
                    sub in {"rebase", "filter-branch", "filter-repo"} or (sub == "stash" and "drop" in gargs) or \
                    (sub == "reflog" and "expire" in gargs):
                a.risk = max(a.risk, Risk.DANGEROUS)
                a.reasons.append(f"git {sub} can discard or rewrite work")
            elif sub in READ_ONLY_GIT and not (sub in {"branch", "tag", "remote", "config"} and len(gargs) > 1 and
                                               not all(x.startswith("-") for x in gargs[1:])):
                pass
            else:
                a.risk = max(a.risk, Risk.WRITE if sub in {"add", "commit", "stash", "switch", "mv", "rm"}
                             else Risk.EXECUTE)
            continue
        if prog == "find":
            if any(x in {"-exec", "-execdir", "-delete", "-ok", "-fprint", "-fls"} for x in argv):
                a.risk = max(a.risk, Risk.EXECUTE)
                a.reasons.append("find with -exec/-delete")
            continue
        if prog in {"sed", "perl"} and any(x.startswith("-i") for x in argv):
            a.risk = max(a.risk, Risk.WRITE)
            a.reasons.append("in-place file edit")
            continue
        if prog in READ_ONLY_PROGRAMS or (len(argv) == 2 and argv[1] in VERSION_FLAGS):
            continue
        if prog in {"rm", "rmdir", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln", "truncate", "tee", "install"}:
            a.risk = max(a.risk, Risk.WRITE)
            a.reasons.append(f"modifies files ({prog})")
            if prog == "rm" and any("r" in x for x in argv[1:] if x.startswith("-")):
                a.reasons.append("recursive delete")
            continue
        if prog in {"kill", "pkill", "killall"}:
            a.risk = max(a.risk, Risk.EXECUTE)
            a.reasons.append("signals other processes")
            continue
        a.risk = max(a.risk, Risk.EXECUTE)
        if f"runs {prog}" not in a.reasons:
            a.reasons.append(f"runs {prog}")
    if a.outside_project:
        a.reasons.append("references paths outside the project")
        if a.risk == Risk.WRITE:
            a.risk = Risk.DANGEROUS
    if a.network:
        a.reasons.append("may access the network")
        a.risk = max(a.risk, Risk.EXECUTE)
    if a.risk == Risk.READ and allow_prefixes is not None:
        a.reasons.append("read-only command")
    return a


def matches_prefix(command: str, prefixes: list[str]) -> bool:
    cmd = command.strip()
    return any(cmd == p or cmd.startswith(p + " ") for p in prefixes)
