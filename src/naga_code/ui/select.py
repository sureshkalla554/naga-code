"""Inline arrow-key menu: ↑/↓ (or j/k) to move, Enter to choose, a number or shortcut key to pick directly."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

ACCENT = "#f28a1e"


@dataclass
class Option:
    value: str
    label: str
    key: str = ""        # optional single-letter shortcut


async def select(options: list[Option], *, cancel_value: str | None = None, default: int = 0) -> str | None:
    """Show the menu below the cursor and return the chosen option's value.

    Esc / Ctrl+C return `cancel_value`. The menu is erased afterwards; callers print the outcome."""
    index = [max(0, min(default, len(options) - 1))]

    def render() -> FormattedText:
        out: list[tuple[str, str]] = []
        for i, opt in enumerate(options):
            if i == index[0]:
                out += [(f"fg:{ACCENT} bold", f"  ❯ {i + 1}. {opt.label}")]
            else:
                out += [("", f"    {i + 1}. {opt.label}")]
            out.append(("", "\n"))
        out.append(("fg:#888888", "  ↑/↓ to move · enter to select · esc to cancel"))
        return FormattedText(out)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    @kb.add("s-tab")
    def _(event):
        index[0] = (index[0] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    @kb.add("tab")
    def _(event):
        index[0] = (index[0] + 1) % len(options)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=options[index[0]].value)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        event.app.exit(result=cancel_value)

    for i, opt in enumerate(options[:9]):
        kb.add(str(i + 1))(lambda event, v=opt.value: event.app.exit(result=v))
        if opt.key and opt.key not in "jk":
            kb.add(opt.key)(lambda event, v=opt.value: event.app.exit(result=v))

    app: Application = Application(layout=Layout(Window(FormattedTextControl(render, show_cursor=False),
                                                        dont_extend_height=True)),
                                   key_bindings=kb, full_screen=False, erase_when_done=True, mouse_support=False)
    return await app.run_async()
