"""Layered configuration: built-in defaults < user config < project config.

Project configuration (``.naga/config.toml``) is treated as untrusted repository
data. It may *tighten* security settings (add deny rules) but cannot loosen
them, and it cannot enable hooks without an explicit trust decision.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "naga"


def config_home() -> Path:
    if os.environ.get("NAGA_HOME"):
        return Path(os.environ["NAGA_HOME"]) / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def data_home() -> Path:
    if os.environ.get("NAGA_HOME"):
        return Path(os.environ["NAGA_HOME"]) / "data"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


DEFAULTS: dict[str, Any] = {
    "general": {
        "default_profile": "coding",
        "compact_ui": False,
        "auto_commit": False,
    },
    "providers": {
        "ollama": {"kind": "ollama", "base_url": "http://127.0.0.1:11434", "enabled": True},
        "llamacpp": {"kind": "llama_cpp", "base_url": "http://127.0.0.1:8080", "enabled": False},
        "vllm": {"kind": "openai_compatible", "base_url": "http://127.0.0.1:8001/v1", "enabled": False},
    },
    "profiles": {
        # Roles, not capability claims: `naga models` / `/doctor` validate them.
        "coding": {"provider": "ollama", "model": "qwen3-coder:latest", "num_ctx": 32768,
                   "temperature": 0.2, "tool_mode": "auto"},
        "fast": {"provider": "ollama", "model": "qwen3-coder:latest", "num_ctx": 16384,
                 "temperature": 0.1, "tool_mode": "auto"},
        "reasoning": {"provider": "ollama", "model": "qwen3.6:27b", "num_ctx": 32768,
                      "temperature": 0.3, "tool_mode": "auto"},
        "vision": {"provider": "ollama", "model": "qwen3.6:27b", "num_ctx": 16384,
                   "temperature": 0.2, "tool_mode": "auto"},
        # Same model as `coding` by default so a delegation does not force a GPU model swap.
        "subagent": {"provider": "ollama", "model": "qwen3-coder:latest", "num_ctx": 32768,
                     "temperature": 0.2, "tool_mode": "auto"},
    },
    "agent": {
        "max_iterations": 40,
        "max_wall_seconds": 1800,
        "max_repeat_failures": 2,
        "require_validation_nudge": True,
        "requirements_check": True,   # re-check the request's requirements before finishing a turn that edited files
    },
    "permissions": {
        # default: reads auto-approved, writes + commands prompt.
        # accept-edits: workspace file edits auto-approved, commands prompt.
        # read-only: nothing but reads.
        "mode": "default",
        "allow_commands": [
            "git status", "git diff", "git log", "git show", "git branch",
            "ls", "pwd", "cat", "head", "tail", "wc", "rg", "grep", "which", "file", "stat", "tree",
        ],
        "deny_commands": [],
        "extra_read_dirs": [],
    },
    "context": {
        "reserve_tokens": 4096,
        "compact_threshold": 0.75,
        "max_tool_output_chars": 12000,
        "keep_recent_tool_results": 8,
        "max_file_read_lines": 2000,
        "max_instruction_chars": 12000,
    },
    "terminal": {
        "default_timeout": 120,
        "max_timeout": 1800,
        "max_output_chars": 200_000,
        "sandbox": "auto",  # auto | none | bwrap
        "shell": "/bin/bash",
    },
    "web": {"enabled": False, "timeout": 20, "max_chars": 40000},
    "mcp": {"connect_timeout": 20, "call_timeout": 120},
    "hooks": [],
    "exclude": [".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", "target", ".next",
                ".cache", "*.egg-info"],
}

# Keys a project-level config is never allowed to set (security-relevant).
PROJECT_FORBIDDEN = {
    ("permissions", "mode"),
    ("permissions", "allow_commands"),
    ("permissions", "extra_read_dirs"),
    ("terminal", "sandbox"),
    ("web", "enabled"),
    ("providers",),
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _sanitize_project(cfg: dict) -> tuple[dict, list[str]]:
    cfg = copy.deepcopy(cfg)
    dropped: list[str] = []
    for path in PROJECT_FORBIDDEN:
        node = cfg
        for key in path[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and path[-1] in node:
            del node[path[-1]]
            dropped.append(".".join(path))
    hooks = cfg.pop("hooks", None)
    if hooks:
        cfg["project_hooks"] = hooks  # gated by trust in the hooks engine
    return cfg, dropped


@dataclass
class Config:
    data: dict[str, Any]
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_root: Path | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def profile(self, name: str | None = None) -> dict[str, Any]:
        name = name or self.get("general.default_profile", "coding")
        profiles = self.section("profiles")
        if name not in profiles:
            raise KeyError(f"unknown model profile '{name}' (known: {', '.join(profiles)})")
        return {"name": name, **profiles[name]}


def user_config_path() -> Path:
    return config_home() / "config.toml"


def project_config_path(root: Path) -> Path:
    return root / ".naga" / "config.toml"


def load_config(project_root: Path | None = None, overrides: dict | None = None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    sources = ["defaults"]
    warnings: list[str] = []
    up = user_config_path()
    if up.is_file():
        try:
            data = deep_merge(data, tomllib.loads(up.read_text()))
            sources.append(str(up))
        except (tomllib.TOMLDecodeError, OSError) as e:
            warnings.append(f"ignored invalid user config {up}: {e}")
    if project_root:
        pp = project_config_path(project_root)
        if pp.is_file():
            try:
                proj, dropped = _sanitize_project(tomllib.loads(pp.read_text()))
                deny = proj.get("permissions", {}).pop("deny_commands", None)
                data = deep_merge(data, proj)
                if deny:  # deny rules are additive
                    data["permissions"]["deny_commands"] = list(data["permissions"]["deny_commands"]) + list(deny)
                sources.append(str(pp))
                for d in dropped:
                    warnings.append(f"project config may not set '{d}' (ignored)")
            except (tomllib.TOMLDecodeError, OSError) as e:
                warnings.append(f"ignored invalid project config {pp}: {e}")
    if overrides:
        data = deep_merge(data, overrides)
        sources.append("command line")
    return Config(data=data, sources=sources, warnings=warnings, project_root=project_root)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot encode {type(v).__name__} as TOML")


def unset_user_value(dotted: str) -> bool:
    """Remove a key (or a whole table) from the user config file. Returns False if it was not set."""
    path = user_config_path()
    if not path.is_file():
        return False
    current: dict = tomllib.loads(path.read_text())
    parts = dotted.split(".")
    node = current
    for p in parts[:-1]:
        node = node.get(p)
        if not isinstance(node, dict):
            return False
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for i in range(len(parts) - 1, 0, -1):   # drop tables left empty
        parent = current
        for p in parts[:i - 1]:
            parent = parent[p]
        if parent.get(parts[i - 1]) == {}:
            del parent[parts[i - 1]]
    path.write_text(dump_toml(current))
    return True


def set_user_value(dotted: str, raw: str) -> Path:
    """Set a scalar/list value in the user config file (minimal TOML writer)."""
    path = user_config_path()
    current: dict = tomllib.loads(path.read_text()) if path.is_file() else {}
    try:
        value: Any = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    parts = dotted.split(".")
    node = current
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(current))
    return path


def dump_toml(data: dict, prefix: str = "") -> str:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)
               and not (isinstance(v, list) and v and isinstance(v[0], dict))}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in data.items() if isinstance(v, list) and v and isinstance(v[0], dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_toml_value(v)}")
    for k, v in tables.items():
        name = f"{prefix}{k}"
        body = dump_toml(v, name + ".")
        if any(not isinstance(x, dict) for x in v.values()) or not v:
            lines.append(f"\n[{name}]")
        lines.append(body)
    for k, items in arrays.items():
        for item in items:
            lines.append(f"\n[[{prefix}{k}]]")
            lines.extend(f"{ik} = {_toml_value(iv)}" for ik, iv in item.items())
    return "\n".join(x for x in lines if x != "").strip() + "\n"
