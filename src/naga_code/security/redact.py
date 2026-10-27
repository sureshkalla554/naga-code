"""Secret detection and redaction for logs, memory and environment scrubbing."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
               r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{4,})\3"),
    re.compile(r"(?i)(://[^/\s:@]+:)([^@\s/]{3,})(@)"),  # credentials in URLs
]

SECRET_ENV_NAME = re.compile(
    r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|SESSION|COOKIE|AUTH)"
    r"|^(AWS_|AZURE_|GCP_|GOOGLE_APPLICATION|OPENAI|ANTHROPIC|HF_|HUGGING)")
SAFE_ENV_NAMES = {"SSH_AUTH_SOCK_DISABLED", "XAUTHORITY", "KEYBOARD", "TOKENIZERS_PARALLELISM"}


def redact(text: str) -> str:
    out = text
    for pat in _PATTERNS:
        if pat.groups >= 4:
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}", out)
        elif pat.groups == 3:
            out = pat.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


def contains_secret(text: str) -> bool:
    return redact(text) != text


def is_secret_env(name: str) -> bool:
    return name not in SAFE_ENV_NAMES and bool(SECRET_ENV_NAME.search(name))
