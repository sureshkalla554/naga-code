"""Mutable UI preferences shared by the renderer, prompts and slash commands (/theme, /color, /statusline...)."""

from __future__ import annotations

THEMES = {
    "dark": {"syntax": "ansi_dark", "accent": "#f28a1e"},
    "light": {"syntax": "ansi_light", "accent": "#c25e00"},
    "dark-high-contrast": {"syntax": "monokai", "accent": "#ffa94d"},
    "light-high-contrast": {"syntax": "default", "accent": "#8a3b00"},
}
PROMPT_COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"]
_PT_COLOR = {"red": "ansired", "blue": "ansiblue", "green": "ansigreen", "yellow": "ansiyellow",
             "purple": "ansimagenta", "orange": "#f28a1e", "pink": "#ff79c6", "cyan": "ansicyan"}

STATE: dict = {"theme": "dark", "syntax": "ansi_dark", "accent": "#f28a1e", "prompt_color": "default",
               "statusline": "", "focus": False}


def set_theme(name: str) -> None:
    if name not in THEMES:
        raise ValueError(f"theme must be one of {', '.join(THEMES)}")
    STATE.update(THEMES[name], theme=name)


def prompt_color() -> str:
    return _PT_COLOR.get(STATE["prompt_color"], "ansimagenta")
