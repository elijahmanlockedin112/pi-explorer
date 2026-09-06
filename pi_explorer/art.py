"""Pi, looked at rather than read.

A number with no pattern, painted onto a wall, looks exactly like noise --
and that is the point. This module turns digits into colour, into a walk,
into falling rain, and into a PNG you can zoom into forever.
"""

from __future__ import annotations

import math
import os
import struct
import sys
import time
import zlib
from pathlib import Path

from .engine import Tape
from .search import BITS_RULES
from .ui import BLOCK, C, COLOR, DIGIT_RGB, UNICODE, bg, fg, term_width

__all__ = [
    "PALETTES", "write_png", "wall_png", "wall_terminal", "walk_points",
    "braille_walk", "walk_svg", "rain", "heatmap",
]

PALETTES: dict[str, list[tuple[int, int, int]]] = {
    # Ten hues you can actually tell apart at one pixel each. Zoom into the
    # wall and every dot is a digit; zoom out and it is indistinguishable
    # from television static, which is the entire point.
    "spectrum": [(0x1B, 0x1F, 0x3B), (0x2E, 0x4A, 0x9E), (0x1F, 0x9E, 0xCF),
                 (0x17, 0xB2, 0x6A), (0xA6, 0xD2, 0x1A), (0xF4, 0xC5, 0x18),
                 (0xF5, 0x8A, 0x1F), (0xEF, 0x41, 0x36), (0xE0, 0x39, 0x9A),
                 (0x8A, 0x4F, 0xFF)],
    "pastel": DIGIT_RGB,
    "fire": [(10, 5, 20), (40, 8, 40), (80, 10, 50), (130, 20, 45),
             (175, 40, 35), (210, 70, 25), (235, 110, 20), (250, 160, 40),
             (255, 205, 100), (255, 245, 200)],
    "ice": [(4, 8, 24), (10, 22, 52), (16, 40, 82), (22, 62, 112),
            (30, 88, 140), (46, 118, 165), (74, 150, 188), (114, 182, 210),
            (166, 212, 230), (225, 245, 252)],
    "mono": [(int(255 * i / 9),) * 3 for i in range(10)],
    "bw": [(15, 15, 20)] * 5 + [(240, 240, 245)] * 5,
    "parity": [(15, 15, 20), (240, 240, 245)] * 5,
}


# ---------------------------------------------------------------------------
# PNG, written by hand because zlib is already in the standard library
# ---------------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path | str, width: int, height: int, rows: list[bytes]) -> Path:
    """Write 8-bit truecolour PNG from a list of raw RGB rows."""
    raw = b"".join(b"\x00" + row for row in rows)
    blob = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))
    path = Path(path)
    path.write_bytes(blob)
    return path


def wall_png(tape: Tape, out: Path | str, cols: int = 1000,
             rows: int | None = None, scale: int = 1, palette: str = "spectrum",
             offset: int = 0, rule: str | None = None) -> tuple[Path, int, int]:
    """Lay pi out in rows and save it as an image.

    Pass `rule` ('half' or 'parity') to collapse pi to black and white first --
    that is the version where shapes become findable, because every pixel is a
    coin flip instead of a ten-sided die.
    """
    lut = [bytes(c) for c in PALETTES.get(palette, DIGIT_RGB)]
    available = (len(tape) - offset) // cols
    rows = min(rows or available, available)
    if rows < 1:
        raise SystemExit("not enough digits for even one row -- compute more")

    data = tape.raw(offset, rows * cols)
    if rule:
        data = data.translate(BITS_RULES[rule])
        lut = [bytes(PALETTES["bw"][0]), bytes(PALETTES["bw"][9])]
        base = 48
    else:
        base = 48

    image_rows: list[bytes] = []
    for r in range(rows):
        chunk = data[r * cols:(r + 1) * cols]
        line = b"".join(lut[b - base] * scale for b in chunk)
        for _ in range(scale):
            image_rows.append(line)
    path = write_png(out, cols * scale, rows * scale, image_rows)
    return path, cols * scale, rows * scale


def wall_terminal(tape: Tape, offset: int = 0, cols: int | None = None,
                  rows: int = 24, rule: str | None = None) -> str:
    """The same wall, in the terminal, as coloured blocks."""
    cols = cols or (term_width() - 2)
    data = tape.raw(offset, cols * rows)
    if rule:
        data = data.translate(BITS_RULES[rule])
    out = []
    for r in range(rows):
        chunk = data[r * cols:(r + 1) * cols]
        if not chunk:
            break
        line = []
        for b in chunk:
            d = b - 48
            if rule:
                line.append(fg(255 if d else 236)
                            + (BLOCK if COLOR else ("#" if d else ".")))
            else:
                # Without colour the wall degrades to the digits themselves,
                # which is still the same picture, just spelled out.
                line.append(fg(_ANSI_RAMP[d]) + (BLOCK if COLOR else str(d)))
        out.append("".join(line) + C.RESET)
    return "\n".join(out)


_ANSI_RAMP = [61, 68, 80, 114, 150, 222, 215, 209, 205, 177]


# ---------------------------------------------------------------------------
# the random walk
# ---------------------------------------------------------------------------

def walk_points(tape: Tape, steps: int, offset: int = 0):
    """Each digit is a heading: 0 through 9 spread around the compass."""
    data = tape.raw(offset, steps)
    cos = [math.cos(d * math.tau / 10) for d in range(10)]
    sin = [math.sin(d * math.tau / 10) for d in range(10)]
    x = y = 0.0
    points = [(0.0, 0.0)]
    for b in data:
        d = b - 48
        x += cos[d]
        y += sin[d]
        points.append((x, y))
    return points


class Braille:
    """A canvas where every terminal cell holds a 2x4 grid of dots."""

    DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

    def __init__(self, width: int, height: int):
        self.w = width * 2
        self.h = height * 4
        self.cells: dict[tuple[int, int], int] = {}
        self.heat: dict[tuple[int, int], float] = {}

    def plot(self, x: int, y: int, heat: float = 0.0) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        key = (x >> 1, y >> 2)
        self.cells[key] = self.cells.get(key, 0) | self.DOTS[y & 3][x & 1]
        self.heat[key] = max(self.heat.get(key, 0.0), heat)

    def render(self, ramp: list[int] | None = None) -> str:
        if not self.cells:
            return ""
        ramp = ramp or _WALK_RAMP
        max_x = max(k[0] for k in self.cells)
        max_y = max(k[1] for k in self.cells)
        min_x = min(k[0] for k in self.cells)
        min_y = min(k[1] for k in self.cells)
        lines = []
        for cy in range(min_y, max_y + 1):
            row = []
            last = None
            for cx in range(min_x, max_x + 1):
                bits = self.cells.get((cx, cy))
                if bits is None:
                    row.append(" ")
                    last = None
                    continue
                shade = ramp[min(len(ramp) - 1,
                                 int(self.heat.get((cx, cy), 0) * len(ramp)))]
                if shade != last:
                    row.append(fg(shade))
                    last = shade
                # Braille gives 8 dots per cell; without it, one blob per cell.
                row.append(chr(0x2800 + bits) if UNICODE else "*")
            lines.append("".join(row) + C.RESET)
        return "\n".join(lines)


_WALK_RAMP = [55, 61, 62, 68, 74, 80, 79, 114, 150, 186, 222, 215, 209, 205, 211, 177]


def braille_walk(tape: Tape, steps: int, offset: int = 0,
                 width: int | None = None, height: int = 30) -> str:
    points = walk_points(tape, steps, offset)
    width = width or (term_width() - 2)
    canvas = Braille(width, height)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span_x = max(1e-9, max(xs) - min(xs))
    span_y = max(1e-9, max(ys) - min(ys))
    scale = min((canvas.w - 1) / span_x, (canvas.h - 1) / span_y)
    ox, oy = min(xs), min(ys)
    total = len(points)
    for i, (x, y) in enumerate(points):
        canvas.plot(int((x - ox) * scale), int((y - oy) * scale), i / total)
    return canvas.render()


def walk_svg(tape: Tape, out: Path | str, steps: int, offset: int = 0,
             stroke: float = 0.6, segment: int = 400) -> Path:
    """The same walk, as a vector file you can print and hang on a wall."""
    points = walk_points(tape, steps, offset)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    pad = 20
    min_x, min_y = min(xs), min(ys)
    w = max(1.0, max(xs) - min_x)
    h = max(1.0, max(ys) - min_y)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{-pad} {-pad} {w+2*pad:.1f} {h+2*pad:.1f}" '
        f'width="1600" height="{1600*(h+2*pad)/(w+2*pad):.0f}">',
        f'<rect x="{-pad}" y="{-pad}" width="{w+2*pad:.1f}" '
        f'height="{h+2*pad:.1f}" fill="#0d0f16"/>',
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    total = len(points)
    for start in range(0, total - 1, segment):
        stop = min(start + segment + 1, total)
        hue = 280 - 280 * (start / max(1, total))
        coords = " ".join(f"{x-min_x:.2f},{y-min_y:.2f}"
                          for x, y in points[start:stop])
        parts.append(f'<polyline points="{coords}" '
                     f'stroke="hsl({hue:.0f} 85% 62%)" '
                     f'stroke-width="{stroke}" opacity="0.9"/>')
    parts.append("</g></svg>")
    path = Path(out)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# digit rain
# ---------------------------------------------------------------------------

def rain(tape: Tape, seconds: float = 8.0, fps: int = 18,
         offset: int = 0) -> None:
    """Matrix rain, except the glyphs are the actual digits of pi in order."""
    import random as _random

    width = term_width()
    height = 24
    columns = []
    cursor = offset
    for _ in range(width):
        columns.append({
            "y": _random.uniform(-height, 0),
            "speed": _random.uniform(0.35, 1.4),
            "length": _random.randint(5, height - 2),
            "at": cursor,
        })
        cursor += height

    sys.stdout.write("\x1b[?25l")
    frames = int(seconds * fps)
    try:
        for _ in range(frames):
            grid = [[" "] * width for _ in range(height)]
            colors = [[0] * width for _ in range(height)]
            for x, col in enumerate(columns):
                col["y"] += col["speed"]
                if col["y"] - col["length"] > height:
                    col["y"] = _random.uniform(-6, 0)
                    col["speed"] = _random.uniform(0.35, 1.4)
                    col["at"] = (col["at"] + 997) % max(1, len(tape) - height)
                head = int(col["y"])
                for k in range(col["length"]):
                    y = head - k
                    if 0 <= y < height:
                        idx = (col["at"] + y) % max(1, len(tape) - 1)
                        grid[y][x] = chr(tape.raw(idx, 1)[0])
                        colors[y][x] = 231 if k == 0 else (
                            121 if k < 3 else (35 if k < 8 else 22))
            out = ["\x1b[H"]
            for y in range(height):
                last = -1
                for x in range(width):
                    ch = grid[y][x]
                    if ch == " ":
                        out.append(" ")
                        last = -1
                        continue
                    if colors[y][x] != last:
                        out.append(f"\x1b[38;5;{colors[y][x]}m")
                        last = colors[y][x]
                    out.append(ch)
                out.append("\x1b[0m\n")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            time.sleep(1.0 / fps)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h" + C.RESET + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# which digit follows which
# ---------------------------------------------------------------------------

def heatmap(grid: list[list[int]]) -> str:
    """Shade a 10x10 transition matrix by how far each cell is from expected."""
    total = sum(sum(row) for row in grid)
    expected = total / 100.0
    shades = [17, 18, 19, 20, 26, 31, 37, 43, 71, 107, 143, 179, 215, 209, 203, 197]
    lines = ["      " + "  ".join(f"{d}" for d in range(10)) +
             f"   {C.GREY}(next digit){C.RESET}"]
    for a in range(10):
        cells = []
        for b in range(10):
            deviation = (grid[a][b] - expected) / math.sqrt(max(expected, 1e-9))
            idx = int((deviation + 4) / 8 * (len(shades) - 1))
            idx = max(0, min(len(shades) - 1, idx))
            cells.append(bg(shades[idx])
                         + ("  " if COLOR else f"{deviation:+3.0f}")
                         + C.RESET)
        lines.append(f"  {C.GREY}{a}{C.RESET}   " + " ".join(cells))
    lines.append(f"{C.GREY}  rows = digit, columns = the digit after it. "
                 f"Flat colour means no digit predicts the next.{C.RESET}")
    return "\n".join(lines)
