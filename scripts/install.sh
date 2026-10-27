#!/usr/bin/env bash
# User-level install of Naga Code with uv (no sudo, nothing outside ~/.local and ~/.config).
#   scripts/install.sh            install `naga` into ~/.local/bin (editable: tracks this checkout)
#   scripts/install.sh --copy     install a non-editable copy
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
if [[ "${1:-}" == "--copy" ]]; then
  uv tool install --force .
else
  uv tool install --force --editable .
fi

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "installed: $bin_dir/naga"
case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "note: add $bin_dir to PATH (e.g. run: uv tool update-shell)";; esac

if [[ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/naga/config.toml" ]]; then
  echo "tip: create a config with: naga config init   (or copy examples/config.example.toml)"
fi
echo "next: naga doctor"
