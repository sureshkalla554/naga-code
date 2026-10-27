---
name: linux-troubleshooting
description: Troubleshoot Linux/Ubuntu environment problems (services, ports, permissions, disk, memory, GPU/CUDA,
  packages) with safe read-only diagnostics first.
use_cases:
- port already in use
- service not starting
- permission denied
- disk full
- CUDA/GPU not visible
inputs:
- the symptom and error text
tools:
- run_command
- read_output
permissions: read-only diagnostics auto/approved; any system change requires explicit approval (elevated)
validation:
- diagnosis based on real command output
- no sudo or system modification without explicit approval
output: Symptom, diagnostic commands with real findings, cause, and the exact remediation commands (marked if they
  need sudo).
---

# Linux troubleshooting
1. Start read-only. Useful probes (pick the relevant ones):
   - processes/ports: `ss -ltnp`, `ps aux --sort=-%mem | head`, `lsof -i :PORT` (if installed)
   - services: `systemctl status NAME --no-pager`, `journalctl -u NAME -n 100 --no-pager`
   - disk/memory: `df -h`, `du -sh * | sort -h | tail`, `free -h`
   - permissions: `ls -la PATH`, `id`, `stat PATH`
   - GPU/CUDA: `nvidia-smi`, `nvcc --version`, `ldconfig -p | grep cuda`
   - packages: `apt-cache policy PKG`, `dpkg -l | grep PKG`
2. Form a diagnosis from the outputs. Quote the decisive lines.
3. Propose remediation commands. Anything with sudo, apt, systemctl changes, or files outside the project is elevated:
   explain the impact and wait for explicit approval. Prefer user-level fixes.
