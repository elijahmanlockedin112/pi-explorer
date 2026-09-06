"""Terminal paint: colours, bars, banners, and other pretty nonsense."""

from __future__ import annotations

import os
import shutil
import sys

__all__ = [
    "C", "COLOR", "UNICODE", "BLOCK", "DOT", "SIGMA", "PI_SYMBOL",
    "DIGIT_COLOR", "DIGIT_RGB", "paint_digits", "term_width", "rule",
    "banner", "commas", "human_time", "human_count", "bar", "spark",
    "verdict", "table", "kv", "warn", "note", "meter", "fg", "bg",
]


def _setup_console() -> bool:
    """Windows consoles need asking nicely before they speak ANSI or UTF-8.

    Returns whether we can safely print block characters and braille; if not,
    the whole program quietly falls back to ASCII rather than crashing on a
    sigma halfway through a report.
    """
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return "utf" in (getattr(sys.stdout, "encoding", "") or "").lower()


UNICODE = _setup_console()
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# Everything visual goes through these so an ASCII-only terminal still works.
BLOCK = "█" if UNICODE else "#"
DOT = "·" if UNICODE else "."
SIGMA = "σ" if UNICODE else "sd"
PI_SYMBOL = "π" if UNICODE else "pi"


class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    ITALIC = "\x1b[3m"
    UNDER = "\x1b[4m"
    RED = "\x1b[38;5;203m"
    ORANGE = "\x1b[38;5;215m"
    YELLOW = "\x1b[38;5;222m"
    GREEN = "\x1b[38;5;114m"
    CYAN = "\x1b[38;5;80m"
    BLUE = "\x1b[38;5;75m"
    PURPLE = "\x1b[38;5;141m"
    PINK = "\x1b[38;5;211m"
    GREY = "\x1b[38;5;244m"
    FAINT = "\x1b[38;5;238m"
    HIT = "\x1b[48;5;205m\x1b[38;5;16m\x1b[1m"

    @classmethod
    def strip(cls) -> None:
        for name in list(vars(cls)):
            if name.isupper():
                setattr(cls, name, "")


if not COLOR:
    C.strip()

# One stable colour per digit so a 3 looks the same in a search hit, in the
# wall, and in the artwork. Small thing, makes the whole tool feel like one
# object instead of ten scripts.
DIGIT_COLOR = [
    "\x1b[38;5;61m",   # 0  indigo
    "\x1b[38;5;68m",   # 1  steel
    "\x1b[38;5;80m",   # 2  cyan
    "\x1b[38;5;114m",  # 3  green
    "\x1b[38;5;150m",  # 4  sage
    "\x1b[38;5;222m",  # 5  sand
    "\x1b[38;5;215m",  # 6  apricot
    "\x1b[38;5;209m",  # 7  coral
    "\x1b[38;5;205m",  # 8  rose
    "\x1b[38;5;177m",  # 9  orchid
]

# Same ten colours as RGB, for the PNG wall.
DIGIT_RGB = [
    (0x5F, 0x5F, 0xAF), (0x5F, 0x87, 0xAF), (0x5F, 0xD7, 0xD7),
    (0x87, 0xD7, 0x87), (0xAF, 0xD7, 0x87), (0xFF, 0xD7, 0x87),
    (0xFF, 0xAF, 0x87), (0xFF, 0x87, 0x5F), (0xFF, 0x5F, 0xAF),
    (0xD7, 0x87, 0xFF),
]

if not COLOR:
    DIGIT_COLOR = [""] * 10


def fg(code: int) -> str:
    """256-colour foreground, or nothing at all when colour is off."""
    return f"[38;5;{code}m" if COLOR else ""


def bg(code: int) -> str:
    return f"[48;5;{code}m" if COLOR else ""


def paint_digits(s: str) -> str:
    """Colour a run of digits, each digit keeping its own shade."""
    if not COLOR:
        return s
    return "".join((DIGIT_COLOR[ord(ch) - 48] + ch) if ch.isdigit() else ch
                   for ch in s) + C.RESET


def term_width(default: int = 96) -> int:
    try:
        return max(60, min(shutil.get_terminal_size().columns, 130))
    except Exception:
        return default


def rule(label: str = "", color: str = C.CYAN) -> str:
    width = term_width()
    if not label:
        return color + "-" * width + C.RESET
    head = f"-- {label} "
    return color + head + "-" * max(0, width - len(head)) + C.RESET


BANNER_ART = r"""
        _        ______ __   __ _____  _      ____  _____  ______ _____
   ____(_)__    |  ____|\ \ / /|  __ \| |    / __ \|  __ \|  ____|  __ \
  |  _ \ |  _|  | |__    \ V / | |__) | |   | |  | | |__) | |__  | |__) |
  | |_) ||_|    |  __|   > <   |  ___/| |   | |  | |  _  /|  __| |  _  /
  |  __/        | |____ / . \  | |    | |___| |__| | | \ \| |____| | \ \
  |_|           |______/_/ \_\ |_|    |______\____/|_|  \_\______|_|  \_\
"""


def banner(version: str = "") -> str:
    shades = [C.PURPLE, C.BLUE, C.CYAN, C.GREEN, C.YELLOW, C.ORANGE]
    lines = BANNER_ART.strip("\n").split("\n")
    art = "\n".join(shades[i % len(shades)] + ln + C.RESET
                    for i, ln in enumerate(lines))
    tail = (f"{C.GREY}   v{version}   "
            f"3.{C.RESET}{paint_digits('14159265358979323846264338327950288419')}"
            f"{C.GREY}...{C.RESET}")
    return art + "\n" + tail


def commas(n) -> str:
    return f"{n:,}"


def human_count(n: float) -> str:
    """Big numbers, spoken out loud."""
    names = [
        (1e63, "vigintillion"), (1e60, "novemdecillion"), (1e57, "octodecillion"),
        (1e54, "septendecillion"), (1e51, "sexdecillion"), (1e48, "quindecillion"),
        (1e45, "quattuordecillion"), (1e42, "tredecillion"), (1e39, "duodecillion"),
        (1e36, "undecillion"), (1e33, "decillion"), (1e30, "nonillion"),
        (1e27, "octillion"), (1e24, "septillion"), (1e21, "sextillion"),
        (1e18, "quintillion"), (1e15, "quadrillion"), (1e12, "trillion"),
        (1e9, "billion"), (1e6, "million"), (1e3, "thousand"),
    ]
    if n < 1000:
        return f"{n:.0f}"
    for scale, name in names:
        if n >= scale:
            return f"{n/scale:.3g} {name}"
    return f"{n:.3g}"


def human_time(sec: float) -> str:
    if sec < 1:
        return f"{sec*1000:.0f}ms"
    if sec < 90:
        return f"{sec:.2f}s"
    if sec < 3600:
        return f"{sec/60:.1f}min"
    return f"{sec/3600:.1f}h"


def bar(frac: float, width: int = 26, fill: str = "#", color: str = C.CYAN) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return color + fill * filled + C.FAINT + "." * (width - filled) + C.RESET


def meter(z: float, span: float = 4.0, width: int = 31) -> str:
    """A deviation gauge: centre is 'exactly as expected', edges are +/- span
    standard deviations. Far better than a bar chart when every value is
    within a hair of the same number."""
    half = width // 2
    pos = max(0, min(width - 1, int(round(half + (z / span) * half))))
    color = (C.GREEN if abs(z) < 2 else C.YELLOW if abs(z) < 3 else C.RED)
    cells = []
    for i in range(width):
        if i == pos:
            cells.append(color + BLOCK + C.RESET)
        elif i == half:
            cells.append(C.GREY + "|" + C.RESET)
        else:
            cells.append(C.FAINT + "-" + C.RESET)
    return "".join(cells)


def spark(values, lo=None, hi=None) -> str:
    glyphs = " .:-=+*#%@"
    if not values:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi <= lo:
        return glyphs[len(glyphs) // 2] * len(values)
    out = []
    for v in values:
        idx = int((v - lo) / (hi - lo) * (len(glyphs) - 1))
        out.append(glyphs[max(0, min(len(glyphs) - 1, idx))])
    return "".join(out)


def verdict(p: float) -> str:
    """A p-value, translated into English."""
    if p != p:  # NaN
        return f"{C.GREY}inconclusive{C.RESET}"
    if p < 0.001:
        return f"{C.RED}BROKEN{C.RESET} {C.GREY}(p={p:.2e}){C.RESET}"
    if p < 0.01:
        return f"{C.ORANGE}suspicious{C.RESET} {C.GREY}(p={p:.4f}){C.RESET}"
    if p < 0.05:
        return f"{C.YELLOW}eyebrow raised{C.RESET} {C.GREY}(p={p:.4f}){C.RESET}"
    return f"{C.GREEN}looks random{C.RESET} {C.GREY}(p={p:.4f}){C.RESET}"


def kv(key: str, value: str, width: int = 24) -> str:
    return f"  {C.GREY}{key:<{width}}{C.RESET} {value}"


def warn(msg: str) -> str:
    return f"{C.ORANGE}!{C.RESET} {msg}"


def note(msg: str) -> str:
    return f"{C.GREY}{msg}{C.RESET}"


def table(headers, rows, aligns=None) -> str:
    """Minimal column-aligned table that ignores ANSI width."""
    import re as _re
    strip = lambda s: _re.sub(r"\x1b\[[0-9;]*m", "", str(s))
    cols = len(headers)
    aligns = aligns or ["<"] * cols
    widths = [len(strip(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:cols]):
            widths[i] = max(widths[i], len(strip(cell)))
    def fmt(cells, bold=False):
        parts = []
        for i, cell in enumerate(cells[:cols]):
            pad = widths[i] - len(strip(cell))
            text = str(cell)
            parts.append((" " * pad + text) if aligns[i] == ">" else (text + " " * pad))
        line = "  ".join(parts)
        return (C.BOLD + line + C.RESET) if bold else line
    out = ["  " + fmt(headers, bold=True),
           "  " + C.FAINT + "  ".join("-" * w for w in widths) + C.RESET]
    out += ["  " + fmt(r) for r in rows]
    return "\n".join(out)
