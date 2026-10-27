"""Task ledger: plan, step status, commands, validations, approvals and scratch notes."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

STEP_STATES = ("pending", "running", "completed", "blocked", "failed")


@dataclass
class PlanStep:
    title: str
    status: str = "pending"
    note: str = ""


@dataclass
class Plan:
    objective: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    files_likely_to_change: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    approved: bool = False

    def render(self) -> str:
        if not self.objective and not self.steps:
            return "(no plan)"
        icon = {"pending": "[ ]", "running": "[>]", "completed": "[x]", "blocked": "[!]", "failed": "[-]"}
        out = [f"Objective: {self.objective}"] if self.objective else []
        if self.acceptance_criteria:
            out.append("Acceptance criteria:\n" + "\n".join(f"  - {c}" for c in self.acceptance_criteria))
        if self.dependencies:
            out.append("Dependencies:\n" + "\n".join(f"  - {d}" for d in self.dependencies))
        if self.files_likely_to_change:
            out.append("Files likely to change: " + ", ".join(self.files_likely_to_change))
        out.append("Steps:")
        out += [f"  {icon.get(s.status, '[?]')} {i}. {s.title}" + (f" — {s.note}" if s.note else "")
                for i, s in enumerate(self.steps, 1)]
        if self.risks:
            out.append("Risks:\n" + "\n".join(f"  - {r}" for r in self.risks))
        if self.validation_commands:
            out.append("Validation: " + "; ".join(self.validation_commands))
        out.append(f"Status: {'approved' if self.approved else 'not yet approved'}")
        return "\n".join(out)


@dataclass
class TaskState:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    started: float = field(default_factory=time.time)
    starting_git_status: str = ""
    plan: Plan = field(default_factory=Plan)
    commands: list[dict[str, Any]] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    turn_marker: int = 0  # index into commands/validations at the start of the current turn

    def record_command(self, command: str, exit_code: int | None, duration: float, kind: str = "command",
                       timed_out: bool = False) -> None:
        self.commands.append({"command": command, "exit_code": exit_code, "duration_s": round(duration, 2),
                              "kind": kind, "timed_out": timed_out, "ts": time.time()})

    def record_validation(self, kind: str, command: str, verdict: str, summary: str | None) -> None:
        self.validations.append({"kind": kind, "command": command, "verdict": verdict, "summary": summary,
                                 "ts": time.time()})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskState":
        plan = d.get("plan", {})
        p = Plan(**{**plan, "steps": [PlanStep(**s) for s in plan.get("steps", [])]})
        return cls(**{**d, "plan": p})
