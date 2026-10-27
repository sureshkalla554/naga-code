"""Permission engine: classify actions, ask for approval, record scoped grants."""

from __future__ import annotations

import itertools
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .commands import SHELL_SPLIT, SUBST, Risk, auto_approvable

Level = Risk  # permission levels share the risk scale


class Choice(Enum):
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    DENY = "deny"
    EDIT = "edit"
    CANCEL = "cancel"


@dataclass
class ApprovalRequest:
    tool: str
    operation: str              # exact command or a precise description of the file operation
    level: Risk
    cwd: str = ""
    reason: str = ""            # why the agent wants to do this (from the model, may be empty)
    impact: str = ""            # what the classifier found
    network: bool = False
    outside_project: bool = False
    elevated: bool = False
    grant_key: str | None = None   # scope for "approve for session"; None = not grantable
    agent: str = "main"
    editable: bool = False      # whether the user may edit the operation (commands)
    preview: str | None = None  # e.g. a diff


@dataclass
class ApprovalResponse:
    choice: Choice
    edited: str | None = None
    note: str | None = None


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]


@dataclass
class Grant:
    id: int
    tool: str
    key: str
    created: float = field(default_factory=time.time)

    def describe(self) -> str:
        return f"#{self.id} {self.tool}: {self.key}"


@dataclass
class Authorization:
    allowed: bool
    how: str                    # auto | approved | session-grant | denied | blocked | cancelled
    reason: str = ""
    edited: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.how == "cancelled"


INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "perl", "ruby", "deno", "bun", "npx", "env",
                 "xargs", "eval", "exec", "sudo", "uvx", "pipx"}

EDIT_TOOLS = {"write_file", "edit_file", "apply_patch", "move_path", "make_directory", "undo_last_change", "delete_file"}


def command_grant_key(command: str) -> str:
    """Scope of a session grant for a command: its program + subcommands, not arbitrary arguments."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    head: list[str] = []
    for tok in argv[:3]:
        if tok.startswith("-") or "/" in tok or "=" in tok or (head and "." in tok):
            break
        head.append(tok)
    # Interpreters run arbitrary code: only the exact command line is grantable.
    if not head or head[-1] in INTERPRETERS or len(head) < len(argv) and head[-1] in {"run", "exec", "x"}:
        return command.strip()
    return " ".join(head)


def is_simple_command(command: str) -> bool:
    return len([s for s in SHELL_SPLIT.split(command) if s.strip()]) == 1 and not SUBST.search(command) \
        and ">" not in command and "<" not in command


class PermissionEngine:
    def __init__(self, mode: str = "default", allow_commands: list[str] | None = None,
                 deny_commands: list[str] | None = None, handler: ApprovalHandler | None = None,
                 max_level: Risk = Risk.DANGEROUS, audit=None):
        if mode not in {"default", "accept-edits", "read-only"}:
            raise ValueError(f"unknown permission mode '{mode}'")
        self.mode = mode
        self.allow_commands = list(allow_commands or [])
        self.deny_commands = list(deny_commands or [])
        self.handler = handler
        self.max_level = max_level
        self.agent_mode = "agent"       # ask | plan | agent (set by the runtime)
        self.grants: list[Grant] = []
        self._ids = itertools.count(1)
        self.audit = audit
        self.decisions: list[dict] = []

    def child(self, max_level: Risk, agent: str) -> "PermissionEngine":
        """A restricted engine for a subagent: never broader than this one."""
        c = PermissionEngine(self.mode, self.allow_commands, self.deny_commands, self.handler,
                             min(max_level, self.max_level), self.audit)
        c.agent_mode = self.agent_mode
        c.grants = self.grants  # share grants the user already made; cannot add broader ones
        c._ids = self._ids
        c.decisions = self.decisions
        c._agent_name = agent  # type: ignore[attr-defined]
        return c

    def precheck(self, req: ApprovalRequest) -> Authorization | None:
        """Decide without asking, or return None if the user must be asked."""
        if req.level >= Risk.BLOCKED:
            return Authorization(False, "blocked", f"blocked: {req.impact or 'catastrophic operation'}")
        if req.level > self.max_level:
            return Authorization(False, "denied", f"'{req.tool}' exceeds the permissions granted to this agent")
        if req.level > Risk.READ and self.agent_mode in {"ask", "plan"}:
            return Authorization(False, "denied", f"{self.agent_mode} mode is read-only; propose the change "
                                                  "instead (the user can switch to agent mode)")
        if req.level > Risk.READ and self.mode == "read-only":
            return Authorization(False, "denied", "permission mode is read-only")
        if req.level == Risk.READ and not req.outside_project:
            return Authorization(True, "auto")
        if req.level == Risk.DANGEROUS:
            return None  # always ask, never session-grantable
        if req.level == Risk.WRITE and self.mode == "accept-edits" and req.tool in EDIT_TOOLS \
                and not req.outside_project:
            return Authorization(True, "auto", "accept-edits mode")
        if req.tool == "run_command" and not req.outside_project and not req.network \
                and auto_approvable(req.operation, self.allow_commands, req.cwd or None):
            return Authorization(True, "auto", "allow-listed command")
        if req.grant_key and not req.outside_project:
            for g in self.grants:
                if g.tool == req.tool and (g.key == req.grant_key or
                                           (req.tool == "run_command"
                                            and auto_approvable(req.operation, [g.key], req.cwd or None))):
                    return Authorization(True, "session-grant", g.describe())
        return None

    async def authorize(self, req: ApprovalRequest) -> Authorization:
        auth = self.precheck(req)
        if auth is None:
            if self.handler is None:
                auth = Authorization(False, "denied", "approval required but no interactive approver is available "
                                                      "(non-interactive session)")
            else:
                resp = await self.handler(req)
                if resp.choice == Choice.APPROVE_ONCE:
                    auth = Authorization(True, "approved")
                elif resp.choice == Choice.APPROVE_SESSION:
                    if req.grant_key and req.level < Risk.DANGEROUS:
                        g = Grant(next(self._ids), req.tool, req.grant_key)
                        self.grants.append(g)
                        auth = Authorization(True, "approved", f"granted for session: {g.describe()}")
                    else:
                        auth = Authorization(True, "approved", "approved once (not grantable)")
                elif resp.choice == Choice.EDIT:
                    auth = Authorization(False, "edited", "user edited the operation", edited=resp.edited)
                elif resp.choice == Choice.CANCEL:
                    auth = Authorization(False, "cancelled", "user cancelled the task")
                else:
                    auth = Authorization(False, "denied", "user denied" + (f": {resp.note}" if resp.note else ""))
        record = {"tool": req.tool, "operation": req.operation[:500], "level": req.level.name,
                  "decision": auth.how, "allowed": auth.allowed, "agent": req.agent}
        self.decisions.append(record)
        if self.audit:
            self.audit.record("permission", **record)
        return auth

    def revoke(self, grant_id: int) -> bool:
        before = len(self.grants)
        self.grants[:] = [g for g in self.grants if g.id != grant_id]
        return len(self.grants) != before
