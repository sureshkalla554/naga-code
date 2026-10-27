"""NagaApp: wires configuration, providers, tools, agent, sessions and services together (UI-agnostic)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from . import git as G
from . import plugins
from .agent import instructions as instr
from .agent import planner
from .agent.context import ContextBudget, ContextManager
from .agent.prompts import PromptParts, build_system_prompt
from .agent.repomap import build_repo_map
from .agent.runtime import Agent, AgentEvent, AgentLimits, TurnResult
from .config import Config, data_home, load_config
from .hooks import HookEngine
from .mcp.manager import MCPManager
from .memory import storage as memory_storage
from .memory.sessions import Session, SessionStore
from .providers import GenerationOptions, Provider, ProviderError, build_provider
from .security.audit import AuditLog
from .security.permissions import ApprovalHandler, PermissionEngine
from .security.sandbox import Sandbox
from .skills import SkillLibrary
from .skills import tools as skill_tools
from .subagents import manager as sub_manager
from .terminal import TerminalEngine
from .tools import filesystem, git_tools, search, terminal_tools, testing, web
from .tools.context import ArtifactStore, ToolContext
from .tools.registry import ToolRegistry
from .workspace import Workspace, find_project_root

MODES = ("ask", "plan", "agent")


class NagaApp:
    def __init__(self, cwd: Path, *, overrides: dict | None = None, profile: str | None = None,
                 approval_handler: ApprovalHandler | None = None, on_event: Callable[[AgentEvent], None] | None = None,
                 notify: Callable[[str, str], None] | None = None, provider: Provider | None = None,
                 model: str | None = None):
        self.cwd = cwd.resolve()
        self.project_root = find_project_root(self.cwd)
        self.config: Config = load_config(self.project_root, overrides)
        self.on_event = on_event or (lambda e: None)
        self.notify = notify or (lambda kind, msg: None)
        self.profile_name = profile or self.config.get("general.default_profile", "coding")
        self._provider_override = provider
        self._model_override = model
        self.providers: dict[str, Provider] = {}
        self.provider_status: tuple[bool, str] = (False, "not checked")

        logs = data_home() / "logs"
        self.audit = AuditLog(logs / f"audit-{time.strftime('%Y%m%d')}.jsonl")
        self.workspace = Workspace(self.project_root, self.config.get("exclude", []),
                                   self.config.get("permissions.extra_read_dirs", []))
        tcfg = self.config.section("terminal")
        self.sandbox = Sandbox(self.project_root, tcfg.get("sandbox", "auto"))
        self.terminal = TerminalEngine(self.project_root, self.sandbox, tcfg.get("shell", "/bin/bash"),
                                       tcfg.get("default_timeout", 120), tcfg.get("max_timeout", 1800),
                                       tcfg.get("max_output_chars", 200_000))
        pcfg = self.config.section("permissions")
        self.permissions = PermissionEngine(pcfg.get("mode", "default"), pcfg.get("allow_commands", []),
                                            pcfg.get("deny_commands", []), approval_handler, audit=self.audit)

        self.registry = ToolRegistry()
        for mod in (filesystem, terminal_tools, search, git_tools, testing, planner, skill_tools, memory_storage):
            mod.register(self.registry)
        if self.config.get("web.enabled"):
            web.register(self.registry)

        self.skills = SkillLibrary(self.project_root, plugins.skill_dirs())
        self.memory = memory_storage.MemoryStore(self.project_root)
        self.hooks = HookEngine(self.config.get("hooks", []), self.config.get("project_hooks", []),
                                self.project_root, self.terminal, self._notify, self.audit)
        self.mcp = MCPManager(self.project_root, self.registry, self.config.get("mcp.connect_timeout", 20),
                              self.config.get("mcp.call_timeout", 120))
        self.subagents = sub_manager.SubagentManager(self.registry, self.model_for_profile,
                                                     self.config.get("subagents", {}), self._forward)
        sub_manager.register(self.registry, list(self.subagents.defs))

        self.sessions = SessionStore(self.project_root)
        self.session: Session = self.sessions.new()
        self.artifacts = ArtifactStore(data_home() / "artifacts" / self.session.id)
        self.ctx = ToolContext(self.workspace, self.config, self.permissions, self.terminal, self.artifacts,
                               self.audit, self.hooks, self.session.task, "main",
                               {"skills": self.skills, "memory": self.memory, "subagents": self.subagents,
                                "mcp": self.mcp, "app": self}, self._notify)
        self.instruction_files = instr.discover(self.project_root, self.cwd,
                                                self.config.get("context.max_instruction_chars", 12000))
        self.repo_map = ""
        self.agent: Agent | None = None
        self.model_name = ""
        self.context_length = 8192
        self.tool_mode = "native"
        self.model_caps: list[str] = []
        self.ide = None   # ide.IDEClient when an editor with the Naga extension is open on this project
        self.pending_images: list[str] = []   # base64 images queued by /image or /paste-image for the next message
        self.prompt_extras: dict[str, str] = {}   # named system-prompt sections (/goal, /output-style, ...)
        self.goal: str | None = None
        self.advisor_model: str | None = None
        self.previous_profile: str | None = None

    # ---- events ---------------------------------------------------------
    def _notify(self, kind: str, msg: str) -> None:
        self.notify(kind, msg)

    def _forward(self, e: AgentEvent) -> None:
        self.on_event(e)

    # ---- providers / models ----------------------------------------------
    def get_provider(self, name: str) -> Provider:
        if self._provider_override is not None:
            return self._provider_override
        if name not in self.providers:
            spec = self.config.section("providers").get(name)
            if spec is None:
                raise ProviderError(f"provider '{name}' is not configured", "See `naga config` / examples/config.example.toml")
            self.providers[name] = build_provider(name, spec)
        return self.providers[name]

    def model_for_profile(self, profile: str) -> dict[str, Any]:
        """Provider + model + options for a profile (used by subagents). Falls back to the main model."""
        try:
            p = self.config.profile(profile)
        except KeyError:
            p = self.config.profile(self.profile_name)
        provider = self.get_provider(p["provider"])
        model = self._model_override or p["model"]
        same = model == self.model_name
        return {"provider": provider, "model": model, "options": self._options(p),
                "tool_mode": self.tool_mode if same else ("text" if p.get("tool_mode") == "text" else "native"),
                "context_length": min(int(p.get("num_ctx", 8192)), self.context_length) if same else int(p.get("num_ctx", 8192))}

    @staticmethod
    def _options(p: dict[str, Any]) -> GenerationOptions:
        return GenerationOptions(temperature=p.get("temperature"), top_p=p.get("top_p"), max_tokens=p.get("max_tokens"),
                                 num_ctx=p.get("num_ctx"), seed=p.get("seed"), think=p.get("think"),
                                 extra=dict(p.get("extra", {})))

    async def configure_model(self, profile: str | None = None, model: str | None = None) -> str:
        if profile:
            self.config.profile(profile)  # validate
            self.profile_name = profile
            self._model_override = None  # an explicit profile switch uses that profile's model
        p = self.config.profile(self.profile_name)
        provider = self.get_provider(p["provider"])
        self.provider_status = await provider.health()
        name = model or self._model_override or p["model"]
        if model:
            self._model_override = model
        info = None
        if self.provider_status[0]:
            try:
                info = await provider.model_info(name)
            except ProviderError as e:
                self._notify("warning", str(e))
            if info is None and self._provider_override is None:
                self._notify("warning", f"model '{name}' is not available from provider '{provider.name}'. "
                                        f"Run `naga models` to list installed models, or pull it (e.g. `ollama pull {name}`).")
        self.model_caps = info.capabilities if info else []
        native_ctx = info.context_length if info and info.context_length else None
        want = int(p.get("num_ctx") or native_ctx or 8192)
        self.context_length = min(want, native_ctx) if native_ctx else want
        mode = p.get("tool_mode", "auto")
        if mode == "auto":
            mode = "native" if (info is None or info.supports_tools is not False) else "text"
        self.tool_mode = mode
        self.model_name = name
        opts = self._options(p)
        opts.num_ctx = self.context_length
        cfg = self.config.section("context")
        cm = ContextManager(ContextBudget(self.context_length, int(cfg.get("reserve_tokens", 4096)),
                                          float(cfg.get("compact_threshold", 0.75))),
                            int(cfg.get("keep_recent_tool_results", 8)), int(cfg.get("max_tool_output_chars", 12000)))
        acfg = self.config.section("agent")
        limits = AgentLimits(int(acfg.get("max_iterations", 40)), float(acfg.get("max_wall_seconds", 1800)),
                             int(acfg.get("max_repeat_failures", 2)), bool(acfg.get("require_validation_nudge", True)),
                             bool(acfg.get("requirements_check", True)))
        history = self.agent.history if self.agent else self.session.messages
        self.agent = Agent(provider, name, self.registry, self.ctx, cm, self.system_prompt, tool_mode=mode,
                           options=opts, limits=limits, on_event=self._forward, runtime_context=self.runtime_context)
        self.agent.history = history
        self.session.model = name
        return name

    async def start(self) -> None:
        self.repo_map = build_repo_map(self.workspace, max_chars=6500)
        self.session.task.starting_git_status = G.status(self.project_root).short()
        await self.configure_model()
        await self.hooks.run("session_start", {"project": str(self.project_root)})
        for st in await self.mcp.auto_connect():
            self._notify("info" if st.status == "connected" else "warning",
                         f"MCP {st.config.name}: {st.status}{(' — ' + st.error) if st.error else ''}")
        if self.config.get("ide.enabled", True):
            await self.connect_ide()

    async def connect_ide(self) -> bool:
        """Connect to an editor running the Naga extension for this project (see ide.py)."""
        from . import ide
        if self.ide is not None:
            await self.ide.aclose()
        self.ide = await ide.connect(self.project_root)
        self.ctx.services["ide"] = self.ide
        if self.ide is not None:
            ide.register_tools(self.registry, self.ide, self.project_root)
            self._notify("info", f"connected to {self.ide.name} (editor diagnostics and diffs available)")
        return self.ide is not None

    # ---- prompt ---------------------------------------------------------
    def system_prompt(self) -> str:
        """Stable across turns, so the backend can reuse its cached prompt prefix (a changed byte near the
        start makes a local model re-read everything: ~6 s per call for 14k tokens on qwen3.6)."""
        return build_system_prompt(PromptParts(
            mode=self.permissions.agent_mode, cwd=str(self.cwd), git="see the runtime note at the end",
            repo_map=self.repo_map, instructions=instr.render(self.instruction_files),
            skills_index=self.skills.index_prompt(), agents_index=self.subagents.index_prompt(),
            memory=self.memory.prompt(), extra=list(self.prompt_extras.values())))

    def runtime_context(self) -> str:
        """State that changes during a task (git status, plan, notes): sent as the last message of each call."""
        parts = [f"git: {G.status(self.project_root).short()}"]
        plan = self.session.task.plan.render()
        if plan and plan != "(no plan)":
            parts.append("current task plan (keep acceptance criteria satisfied):\n" + plan)
        if self.session.task.notes:
            parts.append("task notes:\n" + "\n".join(f"- {n}" for n in self.session.task.notes[-20:]))
        return "\n".join(parts)

    # ---- state ----------------------------------------------------------
    @property
    def mode(self) -> str:
        return self.permissions.agent_mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.permissions.agent_mode = mode
        self.session.mode = mode

    def context_usage(self) -> tuple[int, int]:
        if not self.agent:
            return 0, self.context_length
        msgs, tools = self.agent.build_messages()
        return self.agent.context.estimate(msgs, tools), self.context_length

    def status(self) -> dict[str, Any]:
        used, total = self.context_usage()
        return {"project": str(self.project_root), "cwd": str(self.cwd), "profile": self.profile_name,
                "model": self.model_name, "provider": self.provider_status, "mode": self.mode,
                "git": G.status(self.project_root).short(), "context_used": used, "context_total": total,
                "tool_mode": self.tool_mode, "session": self.session.id, "messages": len(self.session.messages),
                "permission_mode": self.permissions.mode, "sandbox": self.sandbox.describe(),
                "changes": len(self.workspace.changes)}

    # ---- turns ----------------------------------------------------------
    async def run(self, text: str, images: list[str] | None = None) -> TurnResult:
        if self.agent is None:
            await self.configure_model()
        assert self.agent is not None
        self.workspace.current_task = self.session.task.id
        from .mentions import expand_mentions, extract_images
        found, names = extract_images(text, self.workspace)
        images = list(images or []) + self.pending_images + found
        self.pending_images = []
        if images and "vision" not in self.model_caps and self.model_caps:
            self._notify("warning", f"{self.model_name} can't read images; {len(images)} image(s) not sent. "
                                    "Switch to a vision model first (e.g. /model qwen3.6:27b).")
            images = []
        elif names:
            self._notify("info", "attached image(s): " + ", ".join(names))
        text = expand_mentions(text, self.workspace)
        try:
            return await self.agent.run(text, images or None)
        finally:
            self.save()

    def save(self) -> None:
        if self.agent:
            self.session.messages = self.agent.history
            u = self.agent.total_usage
            self.session.usage = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                                  "model_seconds": round(u.duration_s or 0, 1)}
        if self.session.messages:
            self.sessions.save(self.session)
            from . import change_store
            change_store.save(self.project_root, self.session.id, self.workspace.changes)

    async def compact(self, focus: str | None = None) -> str:
        assert self.agent
        note = await self.agent.compact(force=True, focus=focus)
        summary = next((m.content for m in self.agent.history if m.meta.get("summary")), "")
        if summary:
            self.session.summaries.append(summary)
        self.save()
        return note

    def reset(self) -> None:
        self.save()
        self.session = self.sessions.new()
        self.ctx.task = self.session.task
        self.workspace.changes = []   # each session keeps its own change ledger (saved with it)
        self.artifacts.dir = data_home() / "artifacts" / self.session.id
        if self.agent:
            self.agent.history = self.session.messages

    def resume(self, sid: str | None = None) -> Session:
        self.save()
        s = self.sessions.load(self.sessions.resolve_id(sid)) if sid else self.sessions.latest()
        if s is None:
            raise KeyError("no saved sessions for this project")
        self.session = s
        self.ctx.task = s.task
        from . import change_store
        self.workspace.changes = change_store.load(self.project_root, s.id)   # /undo and /rewind after a restart
        if self.workspace.changes:
            self.workspace._next_change = max(c.id for c in self.workspace.changes) + 1
        self.set_mode(s.mode if s.mode in MODES else "agent")
        if self.agent:
            self.agent.history = s.messages
        return s

    async def shutdown(self) -> None:
        try:
            await self.hooks.run("session_end", {"session": self.session.id})
        finally:
            self.save()
            await self.mcp.shutdown()
            await self.terminal.shutdown()
            if self.ide is not None:
                await self.ide.aclose()
            for p in self.providers.values():
                await p.aclose()
