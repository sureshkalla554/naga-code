"""Code intelligence with tree-sitter: definitions (outline / find_symbol), and syntax errors right after an edit.

Grammars come from the bundled ``tree-sitter-<language>`` packages, so nothing is downloaded at runtime. Files in
other languages fall back to the regex search in tools/search.py.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# extension -> (module, language function)
GRAMMARS = {
    ".py": ("tree_sitter_python", "language"), ".pyi": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"), ".mjs": ("tree_sitter_javascript", "language"),
    ".cjs": ("tree_sitter_javascript", "language"), ".jsx": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"), ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"), ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"), ".c": ("tree_sitter_c", "language"), ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"), ".cc": ("tree_sitter_cpp", "language"),
    ".cxx": ("tree_sitter_cpp", "language"), ".hpp": ("tree_sitter_cpp", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"), ".sh": ("tree_sitter_bash", "language"),
    ".bash": ("tree_sitter_bash", "language"), ".rb": ("tree_sitter_ruby", "language"),
    ".php": ("tree_sitter_php", "language_php"),
}
DEFINITION_TYPES = {
    "function_definition", "class_definition", "decorated_definition", "function_declaration", "class_declaration",
    "method_definition", "method_declaration", "interface_declaration", "enum_declaration", "type_alias_declaration",
    "abstract_class_declaration", "constructor_declaration", "record_declaration", "struct_declaration",
    "type_spec", "function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item",
    "struct_specifier", "class_specifier", "namespace_definition", "method", "singleton_method", "class", "module",
    "generator_function_declaration", "trait_declaration",
}
MAX_FILE_BYTES = 2_000_000


@dataclass
class Symbol:
    name: str
    kind: str
    line: int          # 1-based
    end_line: int
    container: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.container}.{self.name}" if self.container else self.name


def supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in GRAMMARS


@lru_cache(maxsize=None)
def _parser(suffix: str):
    try:
        import tree_sitter as ts
        mod, fn = GRAMMARS[suffix]
        return ts.Parser(ts.Language(getattr(importlib.import_module(mod), fn)()))
    except (ImportError, KeyError, AttributeError, ValueError):
        return None


def parse(path: str | Path, source: bytes):
    p = _parser(Path(path).suffix.lower())
    return p.parse(source) if p is not None else None


def _kind(node_type: str) -> str:
    for word, kind in (("interface", "interface"), ("trait", "trait"), ("enum", "enum"), ("struct", "struct"),
                       ("impl", "impl"), ("namespace", "namespace"), ("mod_item", "module"), ("module", "module"),
                       ("class", "class"), ("constructor", "method"), ("method", "method"), ("type", "type"),
                       ("function", "function")):
        if word in node_type:
            return kind
    return node_type


def _name(node) -> str | None:
    n = node.child_by_field_name("name")
    if n is None and node.type == "impl_item":
        n = node.child_by_field_name("type")
    if n is None:   # C/C++ functions: name is inside the declarator chain
        d = node.child_by_field_name("declarator")
        while d is not None and d.type not in ("identifier", "field_identifier", "qualified_identifier",
                                               "destructor_name", "operator_name"):
            d = d.child_by_field_name("declarator") or (d.named_children[0] if d.named_children else None)
        n = d
    return n.text.decode(errors="replace") if n is not None and n.text else None


def symbols(path: str | Path, source: bytes) -> list[Symbol]:
    tree = parse(path, source)
    if tree is None:
        return []
    out: list[Symbol] = []

    def walk(node, container: str) -> None:
        here = container
        if node.type in DEFINITION_TYPES and node.type != "decorated_definition":
            name = _name(node)
            if name:
                out.append(Symbol(name, _kind(node.type), node.start_point[0] + 1, node.end_point[0] + 1, container))
                here = f"{container}.{name}" if container else name
        for child in node.named_children:
            walk(child, here)
    walk(tree.root_node, "")
    return out


def syntax_errors(path: str | Path, source: bytes, limit: int = 3) -> list[tuple[int, int, str]]:
    """(line, column, description) of parse errors; [] when the file parses cleanly or isn't supported."""
    tree = parse(path, source)
    if tree is None or not tree.root_node.has_error:
        return []
    found: list[tuple[int, int, str]] = []

    def walk(node) -> None:
        if len(found) >= limit:
            return
        if node.is_missing:
            found.append((node.start_point[0] + 1, node.start_point[1] + 1, f"missing '{node.type}'"))
            return
        if node.type == "ERROR":
            snippet = node.text.decode(errors="replace").strip().splitlines()[0][:60] if node.text else ""
            found.append((node.start_point[0] + 1, node.start_point[1] + 1, f"syntax error near '{snippet}'"))
            return
        if node.has_error:
            for c in node.children:
                walk(c)
    walk(tree.root_node)
    return found


def undefined_names(path: Path) -> list[str]:
    """Python only, when `ruff` is on PATH: undefined-name errors (static analysis, never runs the code)."""
    ruff = shutil.which("ruff")
    if path.suffix != ".py" or ruff is None:
        return []
    try:
        r = subprocess.run([ruff, "check", "--no-cache", "--quiet", "--output-format", "concise", "--select",
                            "F821,F822,F823", str(path)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip() and ":" in ln][:10]


def check_after_edit(root: Path, rel_paths: list[str]) -> str:
    """Problems in just-edited files that tests might not reach: syntax errors and undefined names."""
    lines = []
    for rel in dict.fromkeys(rel_paths):
        p = (root / rel)
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        for line, col, what in syntax_errors(p, p.read_bytes()):
            lines.append(f"{rel}:{line}:{col}: {what}")
        lines += [ln.replace(str(p), rel) for ln in undefined_names(p)]
    return "\n".join(lines)


def outline_text(rel: str, syms: list[Symbol]) -> str:
    if not syms:
        return f"{rel}: no definitions found"
    rows = [f"{s.line:>5}-{s.end_line:<5} {'  ' * s.container.count('.') + ('  ' if s.container else '')}"
            f"{s.kind} {s.name}" for s in syms]
    return f"{rel}\n" + "\n".join(rows)
