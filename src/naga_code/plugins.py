"""Local plugins: folders under ``~/.config/naga/plugins/<name>/`` that bundle skills and slash commands.

Layout::

    <name>/plugin.toml        optional: name, description, version
    <name>/skills/<skill>/SKILL.md
    <name>/commands/<command>.md   prompt templates ($ARGUMENTS is replaced)

A plugin containing a ``.disabled`` marker file is ignored. Plugins never run code on load: skills and
commands are prompts, so anything they make the agent do still goes through the permission system.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import config_home


@dataclass
class Plugin:
    name: str
    path: Path
    description: str = ""
    version: str = ""

    @property
    def enabled(self) -> bool:
        return not (self.path / ".disabled").exists()

    @property
    def skills(self) -> list[str]:
        d = self.path / "skills"
        return sorted(p.parent.name for p in d.glob("*/SKILL.md")) if d.is_dir() else []

    @property
    def commands(self) -> list[str]:
        d = self.path / "commands"
        return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def plugins_dir() -> Path:
    return config_home() / "plugins"


def load(path: Path) -> Plugin:
    meta: dict = {}
    manifest = path / "plugin.toml"
    if manifest.is_file():
        try:
            meta = tomllib.loads(manifest.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{manifest}: {e}") from e
    return Plugin(str(meta.get("name") or path.name), path, str(meta.get("description", "")), str(meta.get("version", "")))


def installed() -> list[Plugin]:
    d = plugins_dir()
    out = []
    for p in sorted(d.iterdir()) if d.is_dir() else []:
        if p.is_dir():
            try:
                out.append(load(p))
            except ValueError:
                continue
    return out


def enabled() -> list[Plugin]:
    return [p for p in installed() if p.enabled]


def skill_dirs() -> list[Path]:
    return [p.path / "skills" for p in enabled() if (p.path / "skills").is_dir()]


def command_dirs() -> list[tuple[str, Path]]:
    return [(p.name, p.path / "commands") for p in enabled() if (p.path / "commands").is_dir()]


def install(source: Path) -> Plugin:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"not a directory: {source}")
    plugin = load(source)
    if not (plugin.skills or plugin.commands):
        raise ValueError(f"{source} has no skills/*/SKILL.md or commands/*.md")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", plugin.name):
        raise ValueError(f"invalid plugin name '{plugin.name}'")
    dest = plugins_dir() / plugin.name
    if dest.exists():
        raise ValueError(f"plugin '{plugin.name}' is already installed ({dest}); remove it first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return load(dest)


def get(name: str) -> Plugin:
    for p in installed():
        if p.name == name or p.path.name == name:
            return p
    raise ValueError(f"no plugin '{name}' (see /plugin list)")


def set_enabled(name: str, on: bool) -> Plugin:
    p = get(name)
    marker = p.path / ".disabled"
    if on:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()
    return p


def remove(name: str) -> Plugin:
    p = get(name)
    if plugins_dir().resolve() not in p.path.resolve().parents:
        raise ValueError(f"refusing to delete {p.path}: not inside {plugins_dir()}")
    shutil.rmtree(p.path)
    return p
