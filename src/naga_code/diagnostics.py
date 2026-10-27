"""`naga doctor`: environment, provider, model, tool and configuration diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import __version__
from .config import config_home, data_home
from .providers import ProviderError
from .security.sandbox import bwrap_status

if TYPE_CHECKING:
    from .app import NagaApp


@dataclass
class Check:
    status: str   # ok | warn | fail | info
    name: str
    detail: str


def gpu_info() -> list[dict[str, str]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4:
            out.append({"name": parts[0], "total_mib": parts[1], "used_mib": parts[2], "driver": parts[3]})
    return out


def estimate_vram_gb(size_bytes: int | None, num_ctx: int) -> float | None:
    """Rough VRAM need: weights + ~10% overhead + KV cache allowance (heuristic, not exact)."""
    if not size_bytes:
        return None
    return size_bytes / 1e9 * 1.1 + num_ctx / 32768 * 2.0


async def run_doctor(app: "NagaApp") -> list[Check]:
    checks = [Check("info", "naga", f"Naga Code {__version__}, Python {sys.version.split()[0]} ({sys.executable})")]
    for tool, why in (("git", "git integration"), ("rg", "fast code search (falls back to Python search)"),
                      ("uv", "environment management")):
        checks.append(Check("ok" if shutil.which(tool) else "warn", tool,
                            shutil.which(tool) or f"not found — needed for {why}"))
    gpus = gpu_info()
    if gpus:
        for g in gpus:
            free = int(g["total_mib"]) - int(g["used_mib"])
            checks.append(Check("ok", "gpu", f"{g['name']} — {free} MiB free of {g['total_mib']} MiB (driver {g['driver']})"))
    else:
        checks.append(Check("warn", "gpu", "no NVIDIA GPU detected via nvidia-smi; inference will be CPU-only"))
    ok, detail = bwrap_status()
    checks.append(Check("ok" if ok else "info", "sandbox", app.sandbox.describe()))
    for d in (config_home(), data_home()):
        try:
            d.mkdir(parents=True, exist_ok=True)
            writable = os.access(d, os.W_OK)
        except OSError:
            writable = False
        checks.append(Check("ok" if writable else "fail", "storage", f"{d} {'writable' if writable else 'NOT writable'}"))
    for w in app.config.warnings:
        checks.append(Check("warn", "config", w))
    checks.append(Check("info", "config", "sources: " + ", ".join(app.config.sources)))

    for name, spec in app.config.section("providers").items():
        if not spec.get("enabled", True):
            checks.append(Check("info", f"provider:{name}", f"disabled ({spec.get('kind')} at {spec.get('base_url')})"))
            continue
        try:
            ok, detail = await app.get_provider(name).health()
        except ProviderError as e:
            ok, detail = False, str(e)
        checks.append(Check("ok" if ok else "fail", f"provider:{name}", detail))

    installed: dict[str, dict] = {}
    for pname, prof in app.config.section("profiles").items():
        prov = prof.get("provider")
        spec = app.config.section("providers").get(prov, {})
        if not spec.get("enabled", True):
            checks.append(Check("warn", f"profile:{pname}", f"uses disabled provider '{prov}'"))
            continue
        try:
            provider = app.get_provider(prov)
            if prov not in installed:
                installed[prov] = {m.name: m for m in await provider.list_models()}
            info = await provider.model_info(prof["model"])
        except ProviderError as e:
            checks.append(Check("fail", f"profile:{pname}", f"{prof.get('model')}: {e}"))
            continue
        if info is None:
            checks.append(Check("fail", f"profile:{pname}", f"model '{prof['model']}' not installed "
                                                            f"(pull it explicitly, e.g. `ollama pull {prof['model']}`)"))
            continue
        caps = info.capabilities
        tools = "native tools" if "tools" in caps else ("text tool protocol" if caps else "tool support unknown")
        size = installed.get(prov, {}).get(prof["model"])
        need = estimate_vram_gb(size.size_bytes if size else None, int(prof.get("num_ctx", 8192)))
        fit = ""
        if need and gpus:
            free_gb = (int(gpus[0]["total_mib"]) - int(gpus[0]["used_mib"])) / 1024
            fit = (f"; ~{need:.0f} GB needed, {free_gb:.0f} GB free" +
                   (" — will partially run on CPU (slow)" if need > free_gb else ""))
        status = "ok" if "tools" in caps or not caps else "warn"
        checks.append(Check(status, f"profile:{pname}", f"{prof['model']} — ctx {info.context_length or '?'} "
                                                        f"(using {prof.get('num_ctx')}), {tools}"
                                                        f"{', vision' if 'vision' in caps else ''}{fit}"))
    for p in app.skills.problems:
        checks.append(Check("warn", "skill", f"{p.path}: {p.error}"))
    checks.append(Check("ok", "skills", f"{len(app.skills.skills)} skills loaded"))
    for e in app.mcp.errors + app.hooks.errors + app.subagents.errors:
        checks.append(Check("warn", "config", e))
    checks.append(Check("info", "mcp", f"{len(app.mcp.servers)} server(s) configured"))
    return checks
