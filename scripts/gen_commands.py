"""Generate COMMANDS.md from the live command registry: uv run python scripts/gen_commands.py > COMMANDS.md"""
import re
import sys

from naga_code.commands import REGISTRY

GROUPS = [
    ("Everyday", ["help", "exit", "clear", "status", "mode", "model", "custom-model", "undo", "rewind", "btw", "copy"]),
    ("Sessions & conversation", ["resume", "sessions", "rename", "branch", "fork", "recap", "export", "compact",
                                 "context", "autocompact", "cd", "add-dir", "verbose", "last"]),
    ("Models & behaviour", ["fast", "effort", "advisor", "output-style", "provider", "cost", "usage"]),
    ("Planning & reviews (run the agent)", ["init", "plan", "ultraplan", "goal", "tasks", "code-review", "ultrareview",
                                            "security-review", "simplify", "verify", "run", "batch", "deep-research",
                                            "team-onboarding", "dataviz", "pr-comments"]),
    ("Code, tests & git", ["files", "search", "test", "lint", "diff", "git", "commit", "checkpoint", "apply"]),
    ("Background & parallel work", ["background", "list-agents", "stop", "subtask", "delegate", "agents", "loop",
                                    "schedule"]),
    ("Skills, plugins & integrations", ["skills", "skill", "skill-doctor", "reload-skills", "run-skill-generator",
                                        "plugin", "plugin-authoring", "reload-plugins", "mcp", "hooks", "memory"]),
    ("Settings & safety", ["config", "update-config", "permissions", "fewer-permission-prompts", "sandbox", "import",
                           "privacy-settings", "tools"]),
    ("Diagnostics & info", ["doctor", "debug", "stats", "insights", "heapdump", "bug", "feedback", "release-notes",
                            "upgrade", "powerup"]),
    ("Look & feel", ["theme", "color", "vim", "focus", "statusline", "keybindings", "terminal-setup"]),
]

def esc(s):
    return s.replace("|", "\\|")

placed = {n for _, names in GROUPS for n in names}
visible = {n for n, c in REGISTRY.commands.items() if not c.hidden and c.group not in {"project", "plugin"}}
missing = visible - placed
if missing:
    sys.exit(f"commands not placed in a group: {sorted(missing)}")
out = ["# Naga Code commands", "",
       "Type these inside a `naga` session. Typing `/` shows suggestions; `/help <command>` shows details.",
       "Commands marked **agent** start an agent turn, so the model does work (with the usual approvals).", ""]

def anchor(g):  # GitHub style: lowercase, drop punctuation except '-', spaces -> '-'
    return re.sub(r"[^a-z0-9 -]", "", g.lower()).replace(" ", "-")
out.append("Contents: " + " · ".join(f"[{g}](#{anchor(g)})" for g, _ in GROUPS) + " · [Not available](#not-available) · [Command line](#command-line)")
for group, names in GROUPS:
    out += ["", f"## {group}", "", "| command | what it does | aliases |", "|---|---|---|"]
    for n in names:
        c = REGISTRY.commands[n]
        usage = c.usage if c.usage.startswith("/") else f"/{c.name} {c.usage}".strip()
        out.append(f"| `{esc(usage)}` | {esc(c.help)}{' **agent**' if c.starts_agent else ''} | "
                   f"{' '.join(f'`/{a}`' for a in c.aliases) or ''} |")
hidden = sorted((n, c) for n, c in REGISTRY.commands.items() if c.hidden)
out += ["", "## Not available", "",
        "These names exist so typing them gives a clear answer. They need a cloud account or hosted service,",
        "which a local-only agent doesn't have.", "", "| command | why |", "|---|---|"]
out += [f"| `/{n}` | {esc(c.help)} |" for n, c in hidden]
out += ["", "## Your own commands", "",
        "- **Project commands:** each `.naga/commands/<name>.md` file becomes `/<name>`; `$ARGUMENTS` is replaced by",
        "  what you type after the command.",
        "- **Plugins:** `~/.config/naga/plugins/<name>/` with `skills/` and `commands/` folders (see `/plugin`).",
        "- Neither can silently replace a built-in command: a clash is registered as `/project:<name>`.",
        "", "## Keys", "", "| key | action |", "|---|---|",
        "| Enter | send |", "| Alt+Enter or Ctrl+J | new line |", "| Shift+Tab | cycle mode: ask → plan → agent |",
        "| Ctrl+C | cancel the running task |", "| Ctrl+D | exit |", "| Ctrl+L | clear the screen |",
        "| Ctrl+R | search input history |", "| `!command` | run a shell command yourself |",
        "| in menus: ↑/↓, Enter, 1–9, Esc | move, choose, pick directly, cancel |",
        "", "## Command line", "", "```bash",
        "naga                                   # interactive session in the current folder",
        "naga --resume [ID]                     # continue the latest (or a given) session",
        "naga --mode plan|ask|agent             # start in a mode",
        "naga --model qwen3-coder:latest        # use a specific model",
        "naga --profile NAME                    # use a profile, including custom models",
        "naga --permission-mode accept-edits    # file edits without asking (commands still ask)",
        "naga --cwd PATH                        # work on another folder",
        "naga --compact                         # compact UI for small terminals",
        'naga -p "task" [--allow-edits] [--allow-commands PREFIX] [--json]   # one task, no interaction',
        "naga doctor | models | sessions | skills list | mcp list | config [show|path|init]",
        "```", ""]
print("\n".join(out))
