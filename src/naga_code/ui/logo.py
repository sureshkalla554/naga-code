"""The Naga Code tiger-face logo: pixel art drawn with half-block characters (two pixels per cell)."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

# One character per pixel. Rows are paired top/bottom into a single terminal line.
TIGER = [
    ".OO............OO.",
    "OPPO..........OPPO",
    "OPOOOOOOOOOOOOOOPO",
    ".OOOOKKOKKOKKOOOO.",
    "OOKOOOOOKKOOOOOKOO",
    "OKOWWOOKKKKOOWWOKO",
    "OOOEEKOOKKOOKEEOOO",
    "KKOOOOOOOOOOOOOOKK",
    "OOKOOOOWWWWOOOOKOO",
    "KKOWWWWWNNWWWWWOKK",
    "OOWWWWWWNNWWWWWWOO",
    ".OWWWWWKWWKWWWWWO.",
    "..WWWWWWKKWWWWWW..",
    "....WWWWWWWWWW....",
]

PALETTE = {
    "O": "#f28a1e",   # fur
    "K": "#1c1410",   # stripes / pupils
    "W": "#fff3e0",   # muzzle and cheeks
    "E": "#ffd23f",   # eyes
    "P": "#f7b7a3",   # inner ear
    "N": "#d9536a",   # nose
}


def tiger(pixels: list[str] | None = None) -> Text:
    rows = list(pixels or TIGER)
    if len(rows) % 2:
        rows.append("." * len(rows[0]))
    out = Text()
    for top, bottom in zip(rows[0::2], rows[1::2], strict=True):
        for a, b in zip(top, bottom, strict=True):
            fa, fb = PALETTE.get(a), PALETTE.get(b)
            if fa and fb:
                out.append("▀", Style(color=fa, bgcolor=fb))
            elif fa:
                out.append("▀", Style(color=fa))
            elif fb:
                out.append("▄", Style(color=fb))
            else:
                out.append(" ")
        out.append("\n")
    out.rstrip()
    return out
