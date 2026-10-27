"""Skills: folders with a SKILL.md (YAML frontmatter + Markdown instructions) and optional files.

Progressive loading:
1. Only name + description of every valid skill go into the system prompt (the index).
2. The full instructions load when the user invokes the skill or the agent calls load_skill.
3. Reference files load only on demand (read_skill_file).
4. Scripts run through run_command, i.e. the normal permission/timeout path. A skill is
   guidance, never a security boundary.

Discovery order (later wins on name clashes): built-in < user (~/.config/naga/skills) < project (.naga/skills).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import config_home

BUILTIN_DIR = Path(__file__).parent / "builtin"
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path                       # directory
    source: str                      # builtin | user | project
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*")
                      if p.is_file() and p.name != "SKILL.md")


@dataclass
class SkillProblem:
    path: Path
    error: str


def parse_skill(skill_md: Path, source: str) -> Skill:
    text = skill_md.read_text(errors="replace")
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError("missing YAML frontmatter (--- ... ---)")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    name = meta.get("name")
    desc = meta.get("description")
    if not isinstance(name, str) or not NAME_RX.match(name):
        raise ValueError("'name' must be lowercase kebab-case (a-z, 0-9, -)")
    if name != skill_md.parent.name:
        raise ValueError(f"'name' ({name}) must match the directory name ({skill_md.parent.name})")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("'description' is required")
    if len(desc) > 1024:
        raise ValueError("'description' must be at most 1024 characters")
    for key in ("tools", "use_cases", "inputs", "validation", "composes"):
        if key in meta and not isinstance(meta[key], list):
            raise ValueError(f"'{key}' must be a list")
    body = m.group(2).strip()
    if len(body) < 40:
        raise ValueError("skill instructions (Markdown body) are missing or too short")
    return Skill(name, desc.strip(), skill_md.parent, source, meta, body)


class SkillLibrary:
    def __init__(self, project_root: Path | None, extra_dirs: list[Path] | None = None):
        self.dirs: list[tuple[Path, str]] = [(BUILTIN_DIR, "builtin"), (config_home() / "skills", "user")]
        if project_root:
            self.dirs.append((project_root / ".naga" / "skills", "project"))
        self.dirs += [(d, "extra") for d in extra_dirs or []]
        self.skills: dict[str, Skill] = {}
        self.problems: list[SkillProblem] = []
        self.refresh()

    def refresh(self) -> None:
        self.skills.clear()
        self.problems.clear()
        for d, source in self.dirs:
            if not d.is_dir():
                continue
            for md in sorted(d.glob("*/SKILL.md")):
                try:
                    s = parse_skill(md, source)
                except (ValueError, OSError) as e:
                    self.problems.append(SkillProblem(md, str(e)))
                    continue
                self.skills[s.name] = s

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def index_prompt(self) -> str:
        if not self.skills:
            return ""
        lines = ["# Skills",
                 "Reusable playbooks. When a request matches one, call load_skill(name) first and follow it. "
                 "Only load skills that are relevant."]
        lines += [f"- {s.name}: {s.description}" for s in self.skills.values()]
        return "\n".join(lines)

    def render(self, s: Skill, arguments: str = "") -> str:
        meta = s.meta
        parts = [f'<skill name="{s.name}" source="{s.source}" dir="{s.path}">']
        for key, label in (("use_cases", "Use cases"), ("inputs", "Inputs"), ("tools", "Required tools"),
                           ("validation", "Validation criteria")):
            if meta.get(key):
                parts.append(f"{label}: " + "; ".join(map(str, meta[key])))
        for key, label in (("permissions", "Permission expectations"), ("output", "Expected output")):
            if meta.get(key):
                parts.append(f"{label}: {meta[key]}")
        parts.append(s.body)
        if s.files:
            parts.append("Skill files (load with read_skill_file; run scripts with run_command using the absolute "
                         f"path under {s.path}): " + ", ".join(s.files))
        for other in meta.get("composes", []) or []:
            if other in self.skills:
                parts.append(f"This skill composes with '{other}': load it with load_skill if needed.")
        if arguments:
            parts.append(f"Arguments from the user: {arguments}")
        parts.append("</skill>")
        return "\n".join(parts)

    def read_file(self, s: Skill, rel: str, max_chars: int = 20000) -> str:
        p = (s.path / rel).resolve()
        if s.path.resolve() not in p.parents or not p.is_file():
            raise ValueError(f"'{rel}' is not a file in skill {s.name}")
        return p.read_text(errors="replace")[:max_chars]
