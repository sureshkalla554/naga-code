"""Code search and lightweight code intelligence (ripgrep with a pure-Python fallback)."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from .context import ToolContext
from .registry import Tool, ToolRegistry, ToolResult

# Definition patterns per language family; {name} is substituted with the escaped symbol.
DEFINITION_PATTERNS = [
    r"^\s*(async\s+)?def\s+{name}\b",                                   # python
    r"^\s*class\s+{name}\b",                                            # python/js/ts/java/c#/kotlin
    r"^\s*(export\s+)?(default\s+)?(async\s+)?function\*?\s+{name}\b",  # js/ts
    r"^\s*(export\s+)?(const|let|var)\s+{name}\s*[=:]",                 # js/ts
    r"^\s*(export\s+)?(interface|type|enum)\s+{name}\b",                # ts
    r"^\s*func\s+(\([^)]*\)\s*)?{name}\b",                              # go
    r"^\s*type\s+{name}\s+(struct|interface)",                          # go
    r"^\s*(pub(\([^)]*\))?\s+)?(async\s+)?(fn|struct|enum|trait|type|mod)\s+{name}\b",  # rust
    r"^\s*(public|private|protected|internal|static|\s)*[\w<>\[\],\s]+\s+{name}\s*\([^;]*$",  # java/c#/c++
    r"^\s*#define\s+{name}\b",                                          # c
]
SOURCE_GLOBS = ["*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.go", "*.rs", "*.java", "*.kt", "*.cs", "*.c", "*.h",
                "*.cc", "*.cpp", "*.hpp", "*.rb", "*.php", "*.swift", "*.scala", "*.lua", "*.sh"]
IMPORT_PATTERNS = {
    ".py": r"^\s*(from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    ".js": r"(import\s.*from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))",
    ".ts": r"(import\s.*from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))",
    ".go": r"^\s*(import\s+)?\"([\w./-]+)\"",
    ".rs": r"^\s*use\s+([\w:]+)",
}


async def _rg(ctx: ToolContext, args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec("rg", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _py_search(ctx: ToolContext, base: Path, rx: re.Pattern, globs: list[str] | None, limit: int) -> list[str]:
    import fnmatch
    hits: list[str] = []
    for fp in ctx.workspace.walk(base):
        if globs and not any(fnmatch.fnmatch(fp.name, g) or fnmatch.fnmatch(ctx.workspace.rel(fp), g) for g in globs):
            continue
        try:
            if fp.stat().st_size > 2_000_000:
                continue
            text = fp.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{ctx.workspace.rel(fp)}:{i}:{line[:300]}")
                if len(hits) >= limit:
                    return hits
    return hits


async def grep(ctx: ToolContext, pattern: str, path: str = ".", globs: list[str] | None = None,
               ignore_case: bool = False, fixed: bool = False, limit: int = 200, word: bool = False,
               context: int = 0) -> list[str]:
    base = ctx.workspace.resolve(path)
    if shutil.which("rg"):
        args = ["--line-number", "--no-heading", "--color", "never", "--max-columns", "300",
                "--max-columns-preview", "--max-count", str(min(limit, 100))]
        for ex in ctx.workspace.exclude_patterns:
            args += ["--glob", f"!{ex}"]
        for g in globs or []:
            args += ["--glob", g]
        if ignore_case:
            args.append("-i")
        if fixed:
            args.append("-F")
        if word:
            args.append("-w")
        if context:
            args += ["-C", str(context)]
        args += ["-e", pattern, str(base.relative_to(ctx.workspace.root)) if base != ctx.workspace.root else "."]
        code, out, err = await _rg(ctx, args, ctx.workspace.root)
        if code == 2 and err:
            raise ValueError(err.strip().splitlines()[-1])
        return out.splitlines()[:limit]
    flags = re.IGNORECASE if ignore_case else 0
    pat = re.escape(pattern) if fixed else pattern
    if word:
        pat = rf"\b{pat}\b"
    return _py_search(ctx, base, re.compile(pat, flags), globs, limit)


async def search_code(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    limit = a.get("max_results", 100)
    try:
        hits = await grep(ctx, a["pattern"], a.get("path", "."), [a["glob"]] if a.get("glob") else None,
                          a.get("ignore_case", False), a.get("fixed_strings", False), limit,
                          context=a.get("context_lines", 0))
    except (ValueError, re.error) as e:
        return ToolResult.error(f"invalid search: {e}")
    more = f"\n... (limited to {limit} lines; narrow the pattern or path)" if len(hits) >= limit else ""
    return ToolResult(True, "\n".join(hits) + more if hits else "(no matches)", summary=f"{len(hits)} matches")


async def find_symbol(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Parser-based for supported languages (exact definitions incl. methods); regex heuristics for the rest."""
    from .. import codeintel
    want = a["name"].split(".")[-1]
    limit = a.get("max_results", 50)
    files = {h.split(":", 1)[0] for h in await grep(ctx, want, a.get("path", "."), SOURCE_GLOBS, fixed=True, word=True,
                                                     limit=2000)}
    exact, unsupported = [], []
    for rel in sorted(files):
        p = ctx.workspace.resolve(rel)
        if not codeintel.supported(p):
            unsupported.append(rel)
            continue
        try:
            src = p.read_bytes()
        except OSError:
            continue
        for s in codeintel.symbols(p, src):
            if s.name == want and (a["name"] == want or s.qualified.endswith(a["name"])):
                exact.append(f"{rel.removeprefix('./')}:{s.line}: {s.kind} {s.qualified} (lines {s.line}-{s.end_line})")
    if exact and not unsupported:
        return ToolResult(True, "\n".join(exact[:limit]), summary=f"{len(exact)} definitions")
    name = re.escape(want)
    combined = "|".join(f"(?:{p.format(name=name)})" for p in DEFINITION_PATTERNS)
    hits = await grep(ctx, combined, a.get("path", "."), SOURCE_GLOBS, limit=limit)
    supported_files = {r for r in files if r not in unsupported}
    hits = [h for h in hits if h.split(":", 1)[0] not in supported_files]   # parser results are exact there
    merged = exact + hits
    return ToolResult(True, "\n".join(merged[:limit]) or f"(no definition of '{a['name']}' found)",
                      summary=f"{len(merged)} definitions")


async def code_outline(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from .. import codeintel
    p = ctx.workspace.resolve(a["path"])
    if not p.is_file():
        return ToolResult.error(f"not a file: {a['path']}")
    if not codeintel.supported(p):
        return ToolResult.error(f"no parser for {p.suffix or 'this file type'}; use search_code or read_file")
    syms = codeintel.symbols(p, p.read_bytes())
    errs = codeintel.syntax_errors(p, p.read_bytes())
    text = codeintel.outline_text(ctx.workspace.rel(p), syms)
    if errs:
        text += "\n\nsyntax errors:\n" + "\n".join(f"  line {ln}:{col} {w}" for ln, col, w in errs)
    return ToolResult(True, text, summary=f"{len(syms)} definitions")


async def find_references(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    hits = await grep(ctx, a["name"], a.get("path", "."), SOURCE_GLOBS, fixed=True, word=True,
                      limit=a.get("max_results", 200))
    return ToolResult(True, "\n".join(hits) or "(no references)", summary=f"{len(hits)} references")


async def list_imports(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = ctx.workspace.resolve(a["path"])
    rx = IMPORT_PATTERNS.get(p.suffix if p.suffix not in {".jsx", ".tsx", ".mjs"} else ".js")
    if not rx:
        return ToolResult.error(f"import discovery not supported for {p.suffix or 'this file type'}")
    text = ctx.workspace.read_text(p).text
    found = []
    for line in text.splitlines():
        m = re.search(rx, line)
        if m:
            found.append(next((g for g in reversed(m.groups()) if g), line.strip()))
    return ToolResult(True, "\n".join(dict.fromkeys(found)) or "(no imports)", summary=f"{len(found)} imports")


async def find_tests(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    import fnmatch
    pats = ["test_*.py", "*_test.py", "*.test.ts", "*.test.js", "*.spec.ts", "*.spec.js", "*_test.go",
            "*Test.java", "*Tests.cs", "*.test.tsx", "*.spec.tsx"]
    out = []
    for fp in ctx.workspace.walk():
        rel = ctx.workspace.rel(fp)
        if any(fnmatch.fnmatch(fp.name, p) for p in pats) or ("/tests/" in f"/{rel}" and fp.suffix == ".rs"):
            if not a.get("query") or a["query"].lower() in rel.lower():
                out.append(rel)
    return ToolResult(True, "\n".join(out[:300]) or "(no test files found)", summary=f"{len(out)} test files")


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    obj = lambda props, req: {"type": "object", "properties": props, "required": req, "additionalProperties": False}  # noqa: E731
    reg.register(Tool("search_code", "Search file contents with a regex (ripgrep). Use glob to filter files, e.g. "
                                     "'*.py'. Also use it to find error messages.",
                      obj({"pattern": S, "path": S, "glob": S, "ignore_case": {"type": "boolean"},
                           "fixed_strings": {"type": "boolean"}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
                           "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["pattern"]),
                      search_code, category="search"))
    reg.register(Tool("code_outline", "List every function, class, method and type defined in a file, with line "
                                      "ranges (parsed, not guessed). Read the outline before reading big files.",
                      obj({"path": S}, ["path"]), code_outline, category="search"))
    reg.register(Tool("find_symbol", "Find where a function/class/method/type is defined. Accepts Class.method. "
                                     "Parsed for Python, JS/TS, Go, Rust, Java, C/C++, C#, Ruby, PHP, Bash.",
                      obj({"name": S, "path": S, "max_results": {"type": "integer", "minimum": 1}}, ["name"]),
                      find_symbol, category="search"))
    reg.register(Tool("find_references", "Find whole-word references / call sites of a symbol in source files.",
                      obj({"name": S, "path": S, "max_results": {"type": "integer", "minimum": 1}}, ["name"]),
                      find_references, category="search"))
    reg.register(Tool("list_imports", "List the imports/dependencies of a source file (py, js, ts, go, rs).",
                      obj({"path": S}, ["path"]), list_imports, category="search"))
    reg.register(Tool("find_tests", "Discover test files, optionally filtered by a substring.",
                      obj({"query": S}, []), find_tests, category="search"))
