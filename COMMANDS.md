# Naga Code commands

Type these inside a `naga` session. Typing `/` shows suggestions; `/help <command>` shows details.
Commands marked **agent** start an agent turn, so the model does work (with the usual approvals).

Contents: [Everyday](#everyday) · [Sessions & conversation](#sessions--conversation) · [Models & behaviour](#models--behaviour) · [Planning & reviews (run the agent)](#planning--reviews-run-the-agent) · [Code, tests & git](#code-tests--git) · [Background & parallel work](#background--parallel-work) · [Skills, plugins & integrations](#skills-plugins--integrations) · [Settings & safety](#settings--safety) · [Diagnostics & info](#diagnostics--info) · [Look & feel](#look--feel) · [Not available](#not-available) · [Command line](#command-line)

## Everyday

| command | what it does | aliases |
|---|---|---|
| `/help [command]` | List commands, or show help for one command. |  |
| `/exit` | Exit Naga Code (session is saved). | `/quit` |
| `/clear [name]` | Start a new conversation with empty context; the old one stays resumable. A name labels it in /resume. (Ctrl+L clears the screen.) | `/reset` `/new` |
| `/status` | Show session, model, provider, mode, context and task status. |  |
| `/mode [ask\|plan\|agent]` | Show or set the agent mode: ask (read-only), plan (plan first), agent (execute). |  |
| `/model [list\|profile\|model]` | Pick a model with the arrow keys, or switch directly: /model <profile\|model>. /model list shows the tables. |  |
| `/custom-model <name> <port\|url> [path] [--model ID] [--ctx N] [--api-key-env VAR] \| list \| use NAME \| remove NAME` | Use a model server you run yourself: give it a name and its port or URL (e.g. 8000, host:8000, https://server/v1, http://server:8000/generate). |  |
| `/undo` | Revert the agent's last file change if the file was not modified since. |  |
| `/rewind` | Roll code and/or conversation back to before an earlier message (pick it with the arrow keys). |  |
| `/btw <question>` | Ask a quick side question about the session without adding it to the conversation. |  |
| `/copy [N]` | Copy the last (or N-th latest) assistant response to the clipboard. |  |

## Sessions & conversation

| command | what it does | aliases |
|---|---|---|
| `/resume [session-id]` | Resume a saved session (latest if no id; id prefixes work). | `/continue` |
| `/sessions` | List saved sessions for this project. |  |
| `/rename [name]` | Rename the current session (shown in /resume); without a name one is generated. |  |
| `/branch [name]` | Branch the conversation here: continue in a copy, the original stays resumable. |  |
| `/fork [prompt]` | Copy this conversation into a background job (with an optional new instruction); you keep working here. |  |
| `/recap` | Summarize the session so far: goal, what was done, current state and open items. |  |
| `/export [path]` | Export the conversation and task report as Markdown. |  |
| `/compact [instructions]` | Summarize older conversation to free context; optional focus instructions for the summary. |  |
| `/context [all]` | Show context usage, loaded instruction files and compaction state ('all' adds a per-message breakdown). |  |
| `/autocompact [auto\|<tokens>]` | Set when context is compacted automatically: a token count (e.g. 24k) or 'auto' (75%). |  |
| `/cd <path>` | Move this session to another working directory, keeping the conversation. |  |
| `/add-dir <path>` | Give the agent file access to another directory for this session (edits there always ask). |  |
| `/verbose` | Toggle showing full tool output and model thinking. |  |
| `/last` | Show the full output of the last tool result. |  |

## Models & behaviour

| command | what it does | aliases |
|---|---|---|
| `/fast [on\|off]` | Toggle the fast model profile on or off. |  |
| `/effort [low\|medium\|high\|xhigh\|max\|auto\|status]` | Set reasoning effort for thinking models (low → off, medium/high/max → on; gpt-oss uses levels). |  |
| `/advisor [model\|off]` | Let the agent consult a second local model for guidance at key moments (adds an ask_advisor tool). |  |
| `/output-style [style]` | Change how responses are written: default, concise, explanatory or learning. |  |
| `/provider [test]` | List configured providers and test their connections. |  |
| `/cost` | Tokens and model time used in this session (local models: no money is spent). |  |
| `/usage` | This session's token usage plus GPU memory and the models Ollama has loaded. |  |

## Planning & reviews (run the agent)

| command | what it does | aliases |
|---|---|---|
| `/init [--template]` | Create/update NAGA.md project instructions (runs the agent; --template writes a draft offline). **agent** |  |
| `/plan [task]` | Show the plan, or plan a task in plan mode and approve it before implementing. **agent** |  |
| `/ultraplan <task>` | Plan with the reasoning model profile in more depth, then approve and implement. **agent** |  |
| `/goal [condition\|clear]` | Set a completion condition; after each turn the agent keeps working until it is met (max 5 rounds). |  |
| `/tasks` | Show task progress: plan steps, commands run, validations, notes. |  |
| `/code-review [low\|medium\|high\|xhigh\|max\|ultra] [--fix] [--max-findings N] [target]` | Review the current diff (or a branch, path or PR number) for correctness bugs. Effort level low..max; --fix applies the findings afterwards; 'ultra' runs /ultrareview. **agent** | `/review` |
| `/ultrareview [target]` | Deep multi-pass review: several reviewer subagents, then every finding is double-checked. **agent** |  |
| `/security-review [target]` | Security review of the pending changes (read-only). **agent** |  |
| `/simplify [target]` | Review changed code for reuse, simplification and efficiency, then apply the cleanups. **agent** |  |
| `/verify [what]` | Check that the latest change really works by running it, not just its unit tests. **agent** |  |
| `/run [what to check]` | Launch this project's app and exercise it to see a change working. **agent** |  |
| `/batch <instruction>` | Large change in parallel units: plan the units, approve, then each is implemented by a subagent. **agent** |  |
| `/deep-research <question>` | Research a question in depth using explorer subagents (and web_fetch if enabled). **agent** |  |
| `/team-onboarding` | Write an onboarding guide for a new teammate on this repository. **agent** |  |
| `/dataviz <request>` | Create a chart from data with matplotlib. **agent** |  |
| `/pr-comments [PR]` | Fetch a GitHub pull request's review comments (gh CLI) and summarize what they ask for. **agent** |  |

## Code, tests & git

| command | what it does | aliases |
|---|---|---|
| `/files [glob]` | List project files (git-tracked + untracked, honoring ignores), optionally filtered. |  |
| `/search PATTERN [--glob GLOB]` | Search source code (ripgrep regex). |  |
| `/test [target \| command]` | Detect and run tests for real (approval required). Arg: a target or a full command. |  |
| `/lint [command]` | Run the configured linters/type checkers (approval required). |  |
| `/diff [--staged] [path...]` | Show the current git diff (or --staged). |  |
| `/git` | Show git state and the git operations available. |  |
| `/commit [message]` | Prepare and create a git commit after approval. |  |
| `/checkpoint [list \| LABEL]` | Save a recoverable patch of all agent changes (or list checkpoints). |  |
| `/apply PATCHFILE` | Apply a reviewed unified-diff patch file (shows the diff and asks for approval). |  |

## Background & parallel work

| command | what it does | aliases |
|---|---|---|
| `/background [prompt]` | Continue this session as a background job and start fresh here. Background jobs cannot ask for approval, so actions that need it are denied. | `/bg` |
| `/list-agents [id]` | List background jobs started with /background, /fork or /schedule, with recent output. |  |
| `/stop <job>` | Stop a background job. |  |
| `/subtask <task>` | Hand a side task to a subagent; its result is added to this conversation. **agent** |  |
| `/delegate AGENT TASK...` | Run a subagent directly on a bounded task. |  |
| `/agents` | List subagents with their tools, permission ceiling and limits. |  |
| `/loop [interval] <prompt>` | Run a prompt repeatedly at an interval (e.g. /loop 5m run the tests). Ctrl+C stops. **agent** | `/proactive` |
| `/schedule [list \| add "<cron expr>" <prompt> \| rm <id>]` | Run a prompt on a schedule with your user crontab: list \| add "<cron>" <prompt> \| rm <id>. | `/routines` |

## Skills, plugins & integrations

| command | what it does | aliases |
|---|---|---|
| `/skills` | List available skills (built-in, user, project) and invalid skill files. |  |
| `/skill NAME [arguments]` | Show a skill (/skill NAME) or invoke it with arguments (/skill NAME ARGS...). **agent** |  |
| `/skill-doctor` | Check every skill for problems: invalid frontmatter, missing descriptions, broken references. |  |
| `/reload-skills` | Re-scan skill folders (built-in, user, project, plugins). |  |
| `/run-skill-generator [description]` | Turn a repeatable workflow into a reusable Naga skill (.naga/skills/). **agent** |  |
| `/plugin [list \| install PATH \| enable NAME \| disable NAME \| remove NAME]` | Manage local plugins: list \| install <path> \| enable/disable/remove <name>. |  |
| `/plugin-authoring [what it should do]` | Create a Naga plugin (skills and slash commands) you can install with /plugin install. **agent** |  |
| `/reload-plugins` | Reload plugins: their skills and slash commands, plus project commands. |  |
| `/mcp [list \| connect N \| disconnect N \| reconnect N\|all \| enable N\|all \| disable N\|all \| trust N \| tools N \| resources N]` | Inspect/manage MCP servers. |  |
| `/hooks [trust]` | Show configured lifecycle hooks; '/hooks trust' approves project hooks. |  |
| `/memory [list \| add [--user] TEXT \| rm SCOPE ID \| edit SCOPE ID TEXT \| export [PATH] \| clear SCOPE]` | Inspect/manage persistent memory. |  |

## Settings & safety

| command | what it does | aliases |
|---|---|---|
| `/config [path \| get KEY \| set KEY VALUE \| key=value ...]` | Show merged configuration, or set user-level values. | `/settings` |
| `/update-config <request>` | Change settings by describing what you want (e.g. 'auto-approve npm test'). **agent** |  |
| `/permissions [mode default\|accept-edits\|read-only] [allow PREFIX] [revoke ID]` | Show/manage permissions: mode, allow-list, session grants. | `/allowed-tools` |
| `/fewer-permission-prompts` | Find read-only commands you approve often and offer to auto-approve them. |  |
| `/sandbox [auto\|bwrap\|none]` | Show how commands are isolated, and choose the sandbox mode (auto \| bwrap \| none). |  |
| `/import [codex\|gemini\|cursor] [--dry-run] [--yes]` | Import instructions and MCP servers from Codex, Gemini CLI or Cursor setups. |  |
| `/privacy-settings` | Show what data Naga keeps, where, and what (if anything) leaves your machine. |  |
| `/tools` | List tools with their category and permission level. |  |

## Diagnostics & info

| command | what it does | aliases |
|---|---|---|
| `/doctor` | Diagnose providers, models, GPU, tools, sandbox and configuration. | `/checkup` |
| `/debug [description]` | Show where Naga's logs are and recent errors; with a description, diagnose the problem. **agent** |  |
| `/stats` | Usage statistics for this project: sessions, messages, tokens and models per day. |  |
| `/insights` | Report on how you use Naga in this project: tools, failures, approvals and most-edited files. |  |
| `/heapdump` | Write a memory snapshot of the Naga process (object counts, RSS) for debugging. |  |
| `/bug [report]` | Write a bug report with environment details (and optionally the conversation) to a local file. | `/share` |
| `/feedback [report]` | Write feedback with environment details to a local file. |  |
| `/release-notes` | Show what's new in this version of Naga Code. |  |
| `/upgrade` | Reinstall the naga command from its source checkout (picks up dependency changes). |  |
| `/powerup` | Short lessons on Naga features. |  |

## Look & feel

| command | what it does | aliases |
|---|---|---|
| `/theme [dark\|light\|dark-high-contrast\|light-high-contrast]` | Change the color theme. |  |
| `/color [color\|default]` | Set the prompt color for this session (no argument: random). |  |
| `/vim` | Toggle vim-style editing of the input line. |  |
| `/focus` | Toggle focus view: hide tool activity and show only answers and turn summaries. |  |
| `/statusline [command\|off]` | Show a shell command's output in the bottom bar (e.g. /statusline git log -1 --format=%s). |  |
| `/keybindings` | Show keyboard shortcuts. |  |
| `/terminal-setup` | Tips for your terminal: multi-line input, colors and clipboard. |  |

## Not available

These names exist so typing them gives a clear answer. They need a cloud account or hosted service,
which a local-only agent doesn't have.

| command | why |
|---|---|
| `/artifact-capabilities` | Artifacts are a claude.ai feature. |
| `/artifact-diagramming` | Artifacts are a claude.ai feature; /dataviz makes charts locally. |
| `/artifacts` | Artifacts are a claude.ai feature. |
| `/auto-mode-setup` | Naga has no automatic classifier mode; use /permissions and /fewer-permission-prompts. |
| `/autofix-pr` | This runs in the cloud; use /pr-comments and /code-review --fix locally. |
| `/chrome` | Browser control is not built in; it could be added as an MCP server (/mcp). |
| `/design` | Design is a claude.ai feature. |
| `/design-login` | Design is a claude.ai feature. |
| `/design-sync` | Design is a claude.ai feature. |
| `/desktop` | There is no Naga desktop app; Naga runs in your terminal. |
| `/ide` | No IDE extension exists; run naga in your editor's terminal. |
| `/install-github-app` | GitHub apps need a hosted service; the gh CLI works through the agent with approval. |
| `/install-slack-app` | Slack integration needs a hosted service. |
| `/login` | Naga has no account: it runs on local models. |
| `/logout` | Naga has no account: it runs on local models. |
| `/mobile` | There is no Naga mobile app. |
| `/passes` | There are no subscriptions or guest passes. |
| `/radio` | Not available. |
| `/rate-limit-options` | There are no rate limits on local models. |
| `/remote-control` | Continuing a session from another device needs a cloud relay; Naga is local-only. |
| `/remote-env` | Cloud environments are not used; Naga is local-only. |
| `/scroll-speed` | Scrolling is handled by your terminal. |
| `/setup-bedrock` | Naga uses local providers (Ollama, llama.cpp, vLLM): see /provider and /config. |
| `/setup-vertex` | Naga uses local providers (Ollama, llama.cpp, vLLM): see /provider and /config. |
| `/slides` | Slides are a claude.ai feature. |
| `/stickers` | Not available. |
| `/teleport` | There are no cloud sessions to pull in; use /resume for local sessions. |
| `/tui` | Naga has a single inline interface; --compact suits small terminals. |
| `/usage-credits` | There are no credits or plans; local models are free to run. See /usage. |
| `/voice` | Voice input needs a speech-to-text model, which is not set up. |
| `/web-setup` | There is no web version of Naga. |
| `/workflow-authoring` | Use /plugin-authoring or /run-skill-generator to create reusable workflows. |
| `/workflows` | Use project commands (.naga/commands/*.md) or plugins (/plugin) for reusable workflows. |

## Your own commands

- **Project commands:** each `.naga/commands/<name>.md` file becomes `/<name>`; `$ARGUMENTS` is replaced by
  what you type after the command.
- **Plugins:** `~/.config/naga/plugins/<name>/` with `skills/` and `commands/` folders (see `/plugin`).
- Neither can silently replace a built-in command: a clash is registered as `/project:<name>`.

## Keys

| key | action |
|---|---|
| Enter | send |
| Alt+Enter or Ctrl+J | new line |
| Shift+Tab | cycle mode: ask → plan → agent |
| Ctrl+C | cancel the running task |
| Ctrl+D | exit |
| Ctrl+L | clear the screen |
| Ctrl+R | search input history |
| `!command` | run a shell command yourself |
| in menus: ↑/↓, Enter, 1–9, Esc | move, choose, pick directly, cancel |

## Command line

```bash
naga                                   # interactive session in the current folder
naga --resume [ID]                     # continue the latest (or a given) session
naga --mode plan|ask|agent             # start in a mode
naga --model qwen3-coder:latest        # use a specific model
naga --profile NAME                    # use a profile, including custom models
naga --permission-mode accept-edits    # file edits without asking (commands still ask)
naga --cwd PATH                        # work on another folder
naga --compact                         # compact UI for small terminals
naga -p "task" [--allow-edits] [--allow-commands PREFIX] [--json]   # one task, no interaction
naga doctor | models | sessions | skills list | mcp list | config [show|path|init]
```

