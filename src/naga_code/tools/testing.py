"""Validation engine: detect project types from manifests and run tests/linters for real."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..security.commands import Risk
from .context import ToolContext
from .registry import Tool, ToolRegistry, ToolResult
from .terminal_tools import _cwd, command_request, execute_command, render_result


@dataclass
class ProjectProfile:
    kinds: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@lru_cache(maxsize=32)
def _python_test_command(root: Path) -> str:
    """Pick a pytest invocation that works in the *project's* environment, not NAGA's own interpreter."""
    import subprocess
    venv_py = next((root / d / "bin" / "python" for d in (".venv", "venv") if (root / d / "bin" / "python").exists()), None)
    for py in ([str(venv_py)] if venv_py else []) + ["python3"]:
        try:
            if subprocess.run([py, "-c", "import pytest"], cwd=root, capture_output=True, timeout=10).returncode == 0:
                return f"{py if py != 'python3' else 'python3'} -m pytest -q"
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "pytest -q" if shutil.which("pytest") else "python3 -m unittest discover"


def detect_project(root: Path) -> ProjectProfile:
    """Inspect manifests; only suggest commands whose tooling is actually configured/installed."""
    pp = ProjectProfile()
    has = lambda name: (root / name).exists()  # noqa: E731
    if has("pyproject.toml") or has("setup.py") or has("setup.cfg") or has("requirements.txt"):
        pp.kinds.append("python")
        runner = "uv run " if has("uv.lock") and shutil.which("uv") else ("poetry run " if has("poetry.lock") else "")
        data: dict[str, Any] = {}
        if has("pyproject.toml"):
            try:
                data = tomllib.loads((root / "pyproject.toml").read_text())
            except tomllib.TOMLDecodeError:
                pp.notes.append("pyproject.toml is not valid TOML")
        text = json.dumps(data)
        if "pytest" in text or has("pytest.ini") or has("conftest.py") or has("tests"):
            pp.test.append(f"{runner}pytest -q")
        elif has("tests") or has("test"):
            pp.test.append(f"{runner}python -m unittest discover")
        if "ruff" in text or has("ruff.toml") or has(".ruff.toml"):
            pp.lint.append(f"{runner}ruff check .")
        if "mypy" in text or has("mypy.ini"):
            pp.lint.append(f"{runner}mypy .")
        if "flake8" in text or has(".flake8"):
            pp.lint.append(f"{runner}flake8")
    if has("package.json"):
        pp.kinds.append("javascript")
        try:
            scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
        except (json.JSONDecodeError, OSError):
            scripts = {}
        pm = "pnpm" if has("pnpm-lock.yaml") else "yarn" if has("yarn.lock") else "bun" if has("bun.lockb") else "npm"
        if "test" in scripts and "no test specified" not in scripts["test"]:
            pp.test.append(f"{pm} test" if pm != "npm" else "npm test --silent")
        for s in ("lint", "typecheck", "type-check"):
            if s in scripts:
                pp.lint.append(f"{pm} run {s}")
        if "build" in scripts:
            pp.build.append(f"{pm} run build")
        if not (root / "node_modules").exists():
            pp.notes.append("node_modules missing: dependencies need installing before tests run")
    if has("Cargo.toml"):
        pp.kinds.append("rust")
        pp.test.append("cargo test")
        pp.lint.append("cargo clippy --all-targets" if shutil.which("cargo-clippy") else "cargo check")
        pp.build.append("cargo build")
    if has("go.mod"):
        pp.kinds.append("go")
        pp.test.append("go test ./...")
        pp.lint.append("go vet ./...")
        pp.build.append("go build ./...")
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")) or list(root.glob("*/*.csproj")):
        pp.kinds.append("dotnet")
        pp.test.append("dotnet test")
        pp.build.append("dotnet build")
    if has("CMakeLists.txt"):
        pp.kinds.append("cmake")
        pp.build.append("cmake -S . -B build && cmake --build build")
        pp.test.append("ctest --test-dir build --output-on-failure")
    if has("Makefile"):
        mk = (root / "Makefile").read_text(errors="replace")
        pp.kinds.append("make")
        if re.search(r"^test:", mk, re.M):
            pp.test.append("make test")
        if re.search(r"^lint:", mk, re.M):
            pp.lint.append("make lint")
    if not pp.kinds:  # no manifest: fall back to conventional test file names
        py_tests = list(root.glob("test_*.py")) + list(root.glob("*_test.py")) + list(root.glob("tests/test_*.py"))
        if py_tests:
            pp.kinds.append("python")
            pp.test.append(_python_test_command(root))
            pp.notes.append("no manifest found; test command inferred from test file names")
    for kind in pp.kinds:
        tool = {"python": None, "javascript": "node", "rust": "cargo", "go": "go", "dotnet": "dotnet",
                "cmake": "cmake", "make": "make"}[kind]
        if tool and not shutil.which(tool):
            pp.notes.append(f"{tool} is not installed; {kind} commands will fail")
    return pp


_SUMMARY = [
    re.compile(r"=+ (.*(passed|failed|error).*) in [\d.]+s"),       # pytest
    re.compile(r"(Tests?:\s+.*(passed|failed).*)"),                  # jest/vitest
    re.compile(r"(test result: .*)"),                                # cargo
    re.compile(r"^(ok|FAIL)\s+\S+", re.M),                           # go
    re.compile(r"(Ran \d+ tests? in [\d.]+s)"),                      # unittest
    re.compile(r"((Passed|Failed)!\s+-.*)"),                         # dotnet
]


def summarize_test_output(text: str) -> str | None:
    for rx in _SUMMARY:
        found = rx.findall(text)
        if found:
            last = found[-1]
            return (last[0] if isinstance(last, tuple) else last).strip()
    return None


async def detect_project_tool(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    pp = detect_project(ctx.workspace.root)
    text = (f"project types: {', '.join(pp.kinds) or 'unknown'}\n"
            f"test commands: {pp.test or 'none detected'}\nlint commands: {pp.lint or 'none detected'}\n"
            f"build commands: {pp.build or 'none detected'}" + ("\nnotes:\n- " + "\n- ".join(pp.notes) if pp.notes else ""))
    return ToolResult(True, text, data=pp.__dict__, summary=", ".join(pp.kinds) or "unknown project")


def _pick(ctx: ToolContext, a: dict[str, Any], kind: str) -> str:
    if a.get("command"):
        return a["command"]
    pp = detect_project(ctx.workspace.root)
    cmds = pp.test if kind == "test" else pp.lint
    if not cmds:
        raise ValueError(f"no {kind} command detected; pass 'command' explicitly after inspecting the project")
    cmd = cmds[0]
    if a.get("target"):
        cmd += " " + a["target"]
    return cmd


async def _run_validation(ctx: ToolContext, a: dict[str, Any], kind: str) -> ToolResult:
    cmd = _pick(ctx, a, kind)
    res = await execute_command(ctx, cmd, _cwd(ctx, a), a.get("timeout", 600), kind="test" if kind == "test" else "lint")
    summary = summarize_test_output(res.stdout + "\n" + res.stderr)
    verdict = "PASSED" if res.ok else ("TIMED OUT" if res.timed_out else "FAILED")
    if ctx.task:
        ctx.task.record_validation(kind, cmd, verdict, summary)
    head = f"{kind} {verdict}" + (f": {summary}" if summary else "") + "\n"
    return ToolResult(res.ok, head + render_result(ctx, res, f"{kind}: {cmd}"),
                      data={"exit_code": res.exit_code, "verdict": verdict},
                      summary=f"{kind} {verdict}" + (f" — {summary}" if summary else ""))


def register(reg: ToolRegistry) -> None:
    S = {"type": "string"}
    schema = {"type": "object", "additionalProperties": False, "required": [],
              "properties": {"command": S, "target": S, "cwd": S, "timeout": {"type": "number", "minimum": 1,
                                                                                "maximum": 1800}}}
    reg.register(Tool("detect_project", "Detect project type(s) and the configured test/lint/build commands.",
                      {"type": "object", "properties": {}, "additionalProperties": False}, detect_project_tool,
                      category="testing"))
    reg.register(Tool("run_tests", "Run tests and report the real result. Without 'command' the detected test "
                                   "command is used; 'target' narrows it (e.g. a test file). Run the smallest relevant "
                                   "tests first.", schema, lambda c, a: _run_validation(c, a, "test"), Risk.EXECUTE,
                      timeout=1900, category="testing",
                      assess=lambda c, a: command_request(c, "run_command", _pick(c, a, "test"), _cwd(c, a),
                                                          "run tests", floor=Risk.EXECUTE)))
    reg.register(Tool("run_lint", "Run the configured linter/type checker and report real results.", schema,
                      lambda c, a: _run_validation(c, a, "lint"), Risk.EXECUTE, timeout=1900, category="testing",
                      assess=lambda c, a: command_request(c, "run_command", _pick(c, a, "lint"), _cwd(c, a),
                                                          "run lint", floor=Risk.EXECUTE)))
