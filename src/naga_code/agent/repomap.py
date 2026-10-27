"""Shallow repository map used at session start (never the whole repository)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .. import git as G
from ..workspace import Workspace

MANIFESTS = ["pyproject.toml", "setup.py", "requirements.txt", "package.json", "Cargo.toml", "go.mod", "pom.xml",
             "build.gradle", "build.gradle.kts", "CMakeLists.txt", "Makefile", "Dockerfile", "docker-compose.yml",
             "compose.yaml", "Gemfile", "composer.json", "*.sln", "*.csproj", "tsconfig.json", "vite.config.ts",
             "pytest.ini", "tox.ini", "jest.config.js", "vitest.config.ts", ".github/workflows/*.yml"]
DOCS = ["README.md", "README.rst", "README", "ARCHITECTURE.md", "CONTRIBUTING.md", "docs"]
INSTRUCTIONS = ["NAGA.md", "AGENTS.md", "CLAUDE.md"]
LANG = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".c": "C", ".h": "C/C++",
        ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
        ".sh": "Shell", ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".vue": "Vue",
        ".svelte": "Svelte", ".md": "Markdown", ".toml": "TOML", ".yaml": "YAML", ".yml": "YAML", ".json": "JSON"}


def _tree(ws: Workspace, root: Path, depth: int, max_entries: int) -> list[str]:
    lines: list[str] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        visible = [e for e in entries if not ws.is_excluded(e) and not (e.name.startswith(".") and e.name not in
                                                                          {".github", ".naga"})]
        for i, e in enumerate(visible):
            if len(lines) >= max_entries:
                return
            if e.is_dir():
                n = sum(1 for _ in e.iterdir()) if level + 1 >= depth else None
                lines.append("  " * level + e.name + "/" + (f"  ({n} entries)" if n is not None else ""))
                if level + 1 < depth:
                    walk(e, level + 1)
            elif level == 0 or i < 12:
                lines.append("  " * level + e.name)
            elif i == 12:
                lines.append("  " * level + f"... ({len(visible) - 12} more)")

    walk(root, 0)
    return lines


def _definitions(ws: Workspace, files: list[str], budget: int = 2500, max_files: int = 80) -> list[str]:
    """One line per source file: its top-level functions/classes (skips tests; bounded)."""
    from ..codeintel import MAX_FILE_BYTES, supported, symbols
    src = [f for f in files if supported(f) and "test" not in Path(f).name.lower() and "/tests/" not in f"/{f}"]
    src.sort(key=lambda f: (f.count("/"), f))
    lines, used = [], 0
    for rel in src[:max_files]:
        p = ws.root / rel
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            names = [s.name for s in symbols(p, p.read_bytes()) if not s.container]
        except OSError:
            continue
        if not names:
            continue
        line = f"  {rel}: " + ", ".join(names[:12]) + (f" (+{len(names) - 12})" if len(names) > 12 else "")
        if used + len(line) > budget:
            lines.append(f"  ... ({len(src) - len(lines)} more source files)")
            break
        lines.append(line)
        used += len(line)
    return lines


def build_repo_map(ws: Workspace, max_chars: int = 6000) -> str:
    root = ws.root
    out = [f"Project root: {root}"]
    st = G.status(root)
    out.append(f"Git: {st.short()}")
    files = ws.git_files() if st.is_repo else None
    if files is None:
        files = [ws.rel(p) for p in ws.walk(max_files=20000)]
    langs = Counter(LANG[Path(f).suffix] for f in files if Path(f).suffix in LANG)
    if langs:
        out.append("Languages (files): " + ", ".join(f"{k} {v}" for k, v in langs.most_common(8)))
    out.append(f"Files: {len(files)}")
    found = []
    for pat in MANIFESTS:
        found += [ws.rel(p) for p in root.glob(pat)]
    if found:
        out.append("Manifests/config: " + ", ".join(sorted(set(found))[:25]))
    instr = [n for n in INSTRUCTIONS if (root / n).is_file()]
    if instr:
        out.append("Instruction files: " + ", ".join(instr))
    from ..tools.testing import detect_project
    pp = detect_project(root)
    if pp.test or pp.lint:
        out.append(f"Detected test commands: {pp.test or '-'}; lint: {pp.lint or '-'}")
    out.append("Layout:")
    out += ["  " + ln for ln in _tree(ws, root, 2, 80)]
    defs = _definitions(ws, files)
    if defs:
        out.append("Top-level definitions (parsed; use code_outline for a file's full outline):")
        out += defs
    for name in DOCS[:3]:
        p = root / name
        if p.is_file():
            text = p.read_text(errors="replace").strip()
            out.append(f"{name} (excerpt):\n" + "\n".join(text.splitlines()[:25])[:1500])
            break
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (map truncated)"
