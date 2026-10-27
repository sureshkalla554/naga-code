#!/usr/bin/env bash
# Remove the `naga` tool. Configuration and data are kept unless --purge is given.
set -euo pipefail
uv tool uninstall naga-code || true
if [[ -n "${NAGA_HOME:-}" ]]; then cfg="$NAGA_HOME/config" data="$NAGA_HOME/data"
else cfg="${XDG_CONFIG_HOME:-$HOME/.config}/naga" data="${XDG_DATA_HOME:-$HOME/.local/share}/naga"; fi
if [[ "${1:-}" == "--purge" ]]; then
  echo "This deletes $cfg (config, skills, memory) and $data (sessions, logs, checkpoints)."
  read -r -p "Type 'delete' to confirm: " ans
  [[ "$ans" == "delete" ]] && rm -rf -- "$cfg" "$data" && echo "purged" || echo "kept"
else
  echo "kept config ($cfg) and data ($data); use --purge to delete them"
fi
