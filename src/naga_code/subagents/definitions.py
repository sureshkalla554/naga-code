"""Built-in subagent roles. Each has a bounded tool set, permission ceiling, model profile and timeout.

Project/user definitions can be added as TOML under ``[subagents.<name>]`` in config with the same fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..security.commands import Risk

READ_TOOLS = ["list_directory", "read_file", "file_info", "find_files", "search_code", "find_symbol",
              "find_references", "list_imports", "find_tests", "repo_summary", "git_status", "git_diff", "git_log",
              "git_show", "git_compare", "detect_project", "read_output", "load_skill", "read_skill_file"]


@dataclass
class SubagentDef:
    name: str
    description: str
    instructions: str
    tools: list[str] = field(default_factory=lambda: list(READ_TOOLS))
    max_level: Risk = Risk.READ
    profile: str = "subagent"
    max_iterations: int = 20
    timeout: float = 600

    @property
    def read_only(self) -> bool:
        return self.max_level == Risk.READ


BUILTIN: dict[str, SubagentDef] = {d.name: d for d in [
    SubagentDef("explorer", "Fast read-only codebase exploration: locate files, symbols, call sites and summarize how something works.",
                "Explore the repository to answer the task. Search broadly first, then read the specific ranges. "
                "Report concrete file paths and line numbers. Do not speculate beyond what you read.", max_iterations=25),
    SubagentDef("architect", "Read-only design analysis: proposes an implementation approach with trade-offs, affected files and risks.",
                "Analyze the codebase and propose a design for the task: components affected, data flow, interfaces, "
                "step-by-step plan, risks and alternatives. Base everything on code you have read."),
    SubagentDef("security-reviewer", "Read-only security audit of code or a diff with source-to-sink evidence.",
                "Perform a security review of the scope in the task. Load the 'security-review' skill first. Report "
                "only confirmed issues with file:line, data flow and remediation; list what you did not review."),
    SubagentDef("code-reviewer", "Read-only correctness review of a diff or files, producing prioritized findings.",
                "Review the scope in the task for correctness bugs and regressions. Load the 'code-review' skill first."),
    SubagentDef("test-engineer", "Finds relevant tests and runs them (execution needs approval); reports real results and failure analysis.",
                "Identify and run the tests relevant to the task, smallest scope first. Report real results; diagnose "
                "failures from actual output. Do not modify files.",
                tools=READ_TOOLS + ["run_tests", "run_lint", "run_command"], max_level=Risk.EXECUTE),
    SubagentDef("performance-analyst", "Measures and analyzes performance (profilers/benchmarks need approval); no file changes.",
                "Measure and explain performance for the task. Load the 'performance-profiling' skill. Report real numbers.",
                tools=READ_TOOLS + ["run_command"], max_level=Risk.EXECUTE),
    SubagentDef("documentation-writer", "Writes or updates documentation files (edits need approval).",
                "Write the documentation requested. Load the 'documentation' skill. Only edit documentation files "
                "(README, docs/, *.md, docstrings) unless the task says otherwise.",
                tools=READ_TOOLS + ["write_file", "edit_file", "apply_patch"], max_level=Risk.WRITE),
    SubagentDef("implementer", "Implements a bounded, well-specified code change and validates it (edits/commands need approval).",
                "Implement exactly the change described in the task, following project conventions. Read before "
                "editing, keep the diff minimal, run the relevant tests, and report real results.",
                tools=READ_TOOLS + ["write_file", "edit_file", "apply_patch", "make_directory", "delete_file", "run_tests", "run_lint",
                                    "run_command"], max_level=Risk.EXECUTE, max_iterations=30, timeout=1200),
]}


def from_config(name: str, d: dict) -> SubagentDef:
    level = {"read": Risk.READ, "write": Risk.WRITE, "execute": Risk.EXECUTE}.get(d.get("permission", "read"))
    if level is None:
        raise ValueError(f"subagent {name}: permission must be read, write or execute")
    return SubagentDef(name, d.get("description", ""), d.get("instructions", ""), d.get("tools", list(READ_TOOLS)),
                       level, d.get("profile", "subagent"), int(d.get("max_iterations", 20)), float(d.get("timeout", 600)))
