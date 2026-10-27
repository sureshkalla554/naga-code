# Naga Code for VS Code

Use the [Naga Code](https://github.com/sureshkalla554/naga-code) local coding agent from VS Code.

- **Ctrl+Alt+N**, or the **Naga** button in the status bar, opens Naga in a terminal for this workspace.
- **Ctrl+Alt+L**, or right-click → *Naga: Send Selection to Naga*, inserts `@file:lines` into Naga's input, so
  Naga reads exactly those lines.
- While Naga runs, it can see the errors and warnings VS Code reports, such as Pylance or ESLint, and after each
  edit it is told about new problems in that file.
- Edits waiting for your approval open in VS Code's side-by-side diff view.

The extension talks to Naga through a local bridge. It listens on 127.0.0.1 only, every request needs a random
per-window token, and the token is stored in `~/.config/naga/ide/` with permissions 0600.

Settings: `naga.command` (default `naga`) and `naga.extraArgs`, for example `["--model", "qwen3-coder:latest"]`.

Build and install: `python3 build_vsix.py`, then `code --install-extension dist/naga-0.1.0.vsix`.
