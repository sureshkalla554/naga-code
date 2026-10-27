# Naga Code — project instructions

Local-first terminal coding agent (Python 3.12, uv). Source in `src/naga_code/`, tests in `tests/`.

## Commands
- install deps: `uv sync`
- unit + security tests (no GPU/network): `uv run pytest -q`
- live tests against local Ollama: `uv run pytest -m integration`
- lint: `uv run ruff check src tests`
- run: `uv run naga` (interactive) · `uv run naga -p "task"` (headless) · `uv run naga doctor`
- benchmark against the local model: `scripts/benchmark.sh --runs 3`

## Architecture (one line each)
- `providers/` — backends (Ollama, OpenAI-compatible/vLLM, llama.cpp, fake) + text tool-call protocol
- `agent/runtime.py` — the loop: model → tool calls → real results → model, with limits and completion checks
- `tools/registry.py` — the single execution path: validate → assess → hooks → permission → run → audit
- `security/` — command risk classifier, permission engine, sandbox, redaction, audit log
- `workspace.py` — path confinement, ignore rules, stale-file protection, change ledger + undo
- `app.py` — wires everything; UI-agnostic. `ui/` is the Rich/prompt-toolkit front end, `commands.py` the slash commands
- `skills/`, `subagents/`, `mcp/`, `memory/`, `hooks.py`, `checkpoints.py`, `diagnostics.py`

## Conventions
- Every tool goes through `ToolRegistry.execute`; never call handlers directly from new code.
- Tools that modify anything must provide an `assess()` that builds an `ApprovalRequest` (dry-run first).
- Never report success the runtime cannot verify; summaries come from `TaskState`/`Workspace.changes`.
- Tests use `FakeProvider` and the `make_app` fixture; `NAGA_HOME` is isolated per test automatically.
- Keep heavy inference dependencies out of the core install.

## Do not
- Add network calls on the inference path or telemetry.
- Let project-level config loosen permissions, enable hooks or web access without user trust.
