"""`@path` and `@path:10-20` mentions in a request: attach the referenced file (or lines) to the message."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from .workspace import PathError, Workspace

MENTION = re.compile(r"(?<![\w/@])@((?:[\w.\-]+/)*[\w.\-]+)(?::(\d+)(?:-(\d+))?)?(?=$|[\s,;:!?)\]])")
MAX_LINES = 400
MAX_CHARS = 20_000


def expand_mentions(text: str, workspace: Workspace) -> str:
    """Append the content of every `@file` that exists inside the project; unknown names are left alone."""
    blocks, seen = [], set()
    for m in MENTION.finditer(text):
        path, a, b = m.group(1).rstrip("."), m.group(2), m.group(3)
        key = (path, a, b)
        if key in seen:
            continue
        try:
            p = workspace.resolve(path)
        except PathError:
            continue
        if not p.is_file() or p.suffix.lower() in IMAGE_SUFFIXES:
            continue
        try:
            lines = workspace.read_text(p).text.splitlines()
        except (OSError, ValueError):
            continue
        seen.add(key)
        start = max(1, int(a)) if a else 1
        end = min(len(lines), int(b) if b else (start if a else len(lines)))
        end = min(end, start + MAX_LINES - 1)
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))[:MAX_CHARS]
        where = f"lines {start}-{end}" if a else (f"lines 1-{end} of {len(lines)}" if end < len(lines) else "whole file")
        blocks.append(f"--- {workspace.rel(p)} ({where}) ---\n{body}")
    if not blocks:
        return text
    return text + "\n\n[Files referenced with @ (already read for you)]\n" + "\n\n".join(blocks)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMAGE_BYTES = 8_000_000
MAX_IMAGES = 4


def load_image(path: Path) -> str:
    """Base64 of an image file (what vision models get), with basic checks."""
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"{path.name} is not an image (png, jpg, webp, gif)")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
    return base64.b64encode(data).decode()


def extract_images(text: str, workspace: Workspace) -> tuple[list[str], list[str]]:
    """Images mentioned with @name.png inside the project: (base64 images, their paths)."""
    images, names = [], []
    for m in MENTION.finditer(text):
        path = m.group(1).rstrip(".")   # "look at @shot.png." - the full stop ends the sentence
        if Path(path).suffix.lower() not in IMAGE_SUFFIXES or len(images) >= MAX_IMAGES:
            continue
        try:
            p = workspace.resolve(path)
            images.append(load_image(p))
            names.append(workspace.rel(p))
        except (PathError, OSError, ValueError):
            continue
    return images, names
