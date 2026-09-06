"""Finding things inside pi: digits, words, dates, and pictures."""

from __future__ import annotations

import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from .engine import Tape
from .stats import expected_hits, poisson_two_sided, prob_appears

__all__ = [
    "SearchResult", "scan", "count_all", "context_around",
    "TEXT_MODES", "encode_text", "decode_letters", "letter_stream",
    "date_variants", "parse_date",
    "BITS_RULES", "bit_tape", "parse_shape", "SHAPE_LIBRARY", "find_shape",
    "render_shape", "shape_odds",
]


# ---------------------------------------------------------------------------
# digit-string search
# ---------------------------------------------------------------------------

def _max_border(pattern: bytes) -> int:
    """Longest proper prefix that is also a suffix (KMP).

    If this is zero the pattern cannot overlap itself, which lets us count
    occurrences with C-speed bytes.count instead of a Python loop.
    """
    n = len(pattern)
    fail = [0] * n
    k = 0
    for i in range(1, n):
        while k and pattern[i] != pattern[k]:
            k = fail[k - 1]
        if pattern[i] == pattern[k]:
            k += 1
        fail[i] = k
    return fail[-1] if n else 0


@dataclass
class SearchResult:
    needle: str
    searched: int
    first: int = -1            # 0-based offset into the digits after "3."
    count: int = 0
    positions: list[int] = field(default_factory=list)
    truncated: bool = False    # positions list was capped

    @property
    def found(self) -> bool:
        return self.first >= 0

    @property
    def position(self) -> int:
        """1-based position, the way everyone counts pi digits."""
        return self.first + 1

    @property
    def expected(self) -> float:
        return expected_hits(self.searched, len(self.needle))

    @property
    def odds_of_appearing(self) -> float:
        return prob_appears(self.searched, len(self.needle))

    @property
    def p_value(self) -> float:
        return poisson_two_sided(self.count, self.expected)

    @property
    def luck(self) -> float:
        """Ratio of where we found it to where we expected to.

        Below 1 means pi coughed it up early; above 1 means we had to dig.
        """
        if not self.found:
            return float("inf")
        return self.position / (10.0 ** len(self.needle))


def count_all(tape: Tape, needle: str, limit: int | None = None,
              collect: int = 64) -> SearchResult:
    """Every occurrence of `needle` in the first `limit` digits."""
    raw = needle.encode()
    length = len(raw)
    end = min(limit or len(tape), len(tape))
    result = SearchResult(needle=needle, searched=end)
    if not length or length > end:
        return result

    first = tape.find(raw, 0, end)
    result.first = first
    if first < 0:
        return result

    if _max_border(raw) == 0:
        # No self-overlap, so non-overlapping counting is exact -- hand the
        # whole job to C, one 4 MB block at a time.
        step = 1 << 22
        total = 0
        start = 0
        while start < end:
            stop = min(start + step, end)
            block = tape.raw(start, (stop + length - 1) - start)
            total += block.count(raw)
            start = stop
        result.count = total
        pos = first
        while pos >= 0 and len(result.positions) < collect:
            result.positions.append(pos)
            pos = tape.find(raw, pos + 1, end)
        result.truncated = result.count > len(result.positions)
    else:
        pos = first
        total = 0
        while pos >= 0:
            total += 1
            if len(result.positions) < collect:
                result.positions.append(pos)
            pos = tape.find(raw, pos + 1, end)
        result.count = total
        result.truncated = total > len(result.positions)
    return result


def scan(tape: Tape, needle: str, limit: int | None = None,
         collect: int = 64) -> SearchResult:
    digits = re.sub(r"[^0-9]", "", needle)
    if not digits:
        raise ValueError(f"'{needle}' has no digits in it")
    return count_all(tape, digits, limit=limit, collect=collect)


def context_around(tape: Tape, offset: int, length: int,
                   pad: int = 24) -> tuple[str, str, str]:
    """(before, hit, after) around a match, for printing."""
    start = max(0, offset - pad)
    before = tape.get(start, offset - start)
    hit = tape.get(offset, length)
    after = tape.get(offset + length, pad)
    return before, hit, after


# ---------------------------------------------------------------------------
# text hiding in the digits
# ---------------------------------------------------------------------------

TEXT_MODES = ("pairs", "a1z26", "ascii")

_MODE_HELP = {
    "pairs": "every 2 digits = one letter (value mod 26). Dense: 1/26 per letter.",
    "a1z26": "a=01 ... z=26, searched as a raw digit run. 1/100 per letter.",
    "ascii": "3-digit ASCII codes, searched as a raw digit run. 1/1000 per letter.",
}


def mode_help(mode: str) -> str:
    return _MODE_HELP.get(mode, "")


def _clean_letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def encode_text(text: str, mode: str = "a1z26") -> str:
    """Turn text into the digit string we go looking for."""
    if mode == "a1z26":
        return "".join(f"{ord(ch) - 96:02d}" for ch in _clean_letters(text))
    if mode == "ascii":
        return "".join(f"{ord(ch):03d}" for ch in text)
    raise ValueError(f"mode {mode!r} is not a raw digit encoding")


def letter_stream(tape: Tape, limit: int | None = None) -> str:
    """Read pi two digits at a time, each pair becoming a letter.

    Pi stops being a number here and becomes a 500,000 page book of gibberish
    that happens to contain every word you will ever write.
    """
    end = min(limit or len(tape), len(tape))
    end -= end % 2
    raw = tape.raw(0, end)
    out = bytearray(end // 2)
    for i in range(0, end, 2):
        value = (raw[i] - 48) * 10 + (raw[i + 1] - 48)
        out[i >> 1] = 97 + value % 26
    return out.decode("ascii")


def decode_letters(tape: Tape, digit_offset: int, n_letters: int) -> str:
    """Decode a run of pi starting at a digit offset into pair-letters."""
    raw = tape.raw(digit_offset, n_letters * 2)
    return "".join(chr(97 + ((raw[i] - 48) * 10 + (raw[i + 1] - 48)) % 26)
                   for i in range(0, len(raw) - 1, 2))


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

_MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october "
    "november december".split())}


def parse_date(text: str) -> tuple[int, int, int]:
    """Accept 2006-07-09, 7/9/2006, 'July 9 2006', '9 July 2006'."""
    text = text.strip().lower()
    iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", text)
    if iso:
        return int(iso[1]), int(iso[2]), int(iso[3])

    named = re.match(r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", text)
    if named and named[1][:3] in {m[:3] for m in _MONTHS}:
        month = next(v for k, v in _MONTHS.items() if k.startswith(named[1][:3]))
        return int(named[3]), month, int(named[2])

    named2 = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?,?\s+(\d{4})$", text)
    if named2 and named2[2][:3] in {m[:3] for m in _MONTHS}:
        month = next(v for k, v in _MONTHS.items() if k.startswith(named2[2][:3]))
        return int(named2[3]), month, int(named2[1])

    slash = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", text)
    if slash:
        year = int(slash[3])
        if year < 100:
            year += 2000 if year < 30 else 1900
        return year, int(slash[1]), int(slash[2])

    raise ValueError(f"could not read {text!r} as a date")


def date_variants(year: int, month: int, day: int) -> dict[str, str]:
    """Every reasonable way to write a date as digits."""
    yy = f"{year % 100:02d}"
    return {
        "M/D/YYYY": f"{month}{day}{year}",
        "MM/DD/YYYY": f"{month:02d}{day:02d}{year}",
        "MM/DD/YY": f"{month:02d}{day:02d}{yy}",
        "DD/MM/YYYY": f"{day:02d}{month:02d}{year}",
        "DD/MM/YY": f"{day:02d}{month:02d}{yy}",
        "YYYY-MM-DD": f"{year}{month:02d}{day:02d}",
        "M/D (no year)": f"{month}{day}",
    }


# ---------------------------------------------------------------------------
# pictures hiding in the digits
# ---------------------------------------------------------------------------

BITS_RULES = {
    # Low half of the digits is off, high half is on. A clean coin flip.
    "half": bytes.maketrans(b"0123456789", b"0000011111"),
    # Even/odd. Also a coin flip, but scrambles neighbouring digits differently.
    "parity": bytes.maketrans(b"0123456789", b"0101010101"),
}


def bit_tape(tape: Tape, limit: int | None = None, rule: str = "half") -> bytes:
    """Collapse pi to one bit per digit: b'0110100...'."""
    if rule not in BITS_RULES:
        raise ValueError(f"unknown bit rule {rule!r}")
    end = min(limit or len(tape), len(tape))
    return tape.raw(0, end).translate(BITS_RULES[rule])


SHAPE_LIBRARY = {
    "square3": ["###", "###", "###"],
    "square4": ["####", "####", "####", "####"],
    "square5": ["#####", "#####", "#####", "#####", "#####"],
    "box": ["####", "#..#", "#..#", "####"],
    "x": ["#...#", ".#.#.", "..#..", ".#.#.", "#...#"],
    "plus": [".#.", "###", ".#."],
    "checker": ["#.#.", ".#.#", "#.#.", ".#.#"],
    "heart": [".#.#.", "#####", "#####", ".###.", "..#.."],
    "smiley": ["#....#", "......", "#....#", "......", "#....#", ".####."],
    "pi": ["#####", ".#.#.", ".#.#.", ".#.#."],
    "arrow": ["..#..", ".###.", "#.#.#", "..#..", "..#.."],
    "diamond": ["..#..", ".###.", "#####", ".###.", "..#.."],
    "amongus": [".###.", "####.", "####.", ".#.#."],
}


def parse_shape(spec: str) -> list[str]:
    """Read a shape from 'a/b/c' or '##../.##.' or a multi-line block.

    '#', '1', 'X' and '*' mean on; anything else means off.
    """
    if spec in SHAPE_LIBRARY:
        rows = list(SHAPE_LIBRARY[spec])
    else:
        rows = [r for r in re.split(r"[/|\n]", spec.strip()) if r.strip()]
    if not rows:
        raise ValueError("empty shape")
    width = max(len(r) for r in rows)
    out = []
    for row in rows:
        out.append("".join("1" if ch in "#1Xx*" else "0"
                           for ch in row.ljust(width)))
    return out


def shape_odds(shape: list[str], n_digits: int, widths: int) -> tuple[float, float]:
    """(expected matches, digits needed for an even-money shot)."""
    cells = len(shape) * len(shape[0])
    per_placement = 0.5 ** cells
    placements = max(0, n_digits) * widths
    needed = (0.693 / per_placement) / max(1, widths)
    return placements * per_placement, needed


# Workers keep their own copy of the bit tape so we never pickle megabytes.
_BITS: bytes | None = None


def _shape_init(path: str, limit: int, rule: str) -> None:
    global _BITS
    with open(path, "rb") as fh:
        _BITS = fh.read(limit).translate(BITS_RULES[rule])


def _shape_scan(job):
    """Earliest match of `shape` when pi is reflowed into rows `width` wide."""
    width, shape = job
    bits = _BITS
    height = len(shape)
    span = len(shape[0])
    if span > width or bits is None:
        return None
    head = shape[0].encode()
    rows = [r.encode() for r in shape[1:]]
    last_start = len(bits) - ((height - 1) * width + span)
    if last_start < 0:
        return None

    pos = bits.find(head, 0, last_start + span)
    while pos != -1 and pos <= last_start:
        # Reject matches that would wrap around the right edge of the grid --
        # on screen those are two half-shapes, not the shape.
        if pos % width + span <= width:
            for i, row in enumerate(rows, start=1):
                base = pos + i * width
                if bits[base:base + span] != row:
                    break
            else:
                return width, pos
        pos = bits.find(head, pos + 1, last_start + span)
    return None


def find_shape(tape: Tape, shape: list[str], limit: int | None = None,
               min_width: int = 0, max_width: int = 128,
               rule: str = "half", workers: int = 1):
    """Hunt for a shape at every row width, return the shallowest hit.

    The same digits reflowed into a different row width are a different
    picture, so scanning widths multiplies your chances -- and every width is
    an independent search, which is exactly what spare cores are for.
    """
    end = min(limit or len(tape), len(tape))
    span = len(shape[0])
    widths = [w for w in range(max(span, min_width or span), max_width + 1)]
    if not widths:
        return None, 0

    jobs = [(w, shape) for w in widths]
    hits = []
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers, initializer=_shape_init,
                                 initargs=(str(tape.path), end, rule)) as pool:
            for hit in pool.map(_shape_scan, jobs, chunksize=1):
                if hit:
                    hits.append(hit)
    else:
        _shape_init(str(tape.path), end, rule)
        for job in jobs:
            hit = _shape_scan(job)
            if hit:
                hits.append(hit)

    if not hits:
        return None, len(widths)
    return min(hits, key=lambda h: h[1]), len(widths)


def render_shape(tape: Tape, width: int, offset: int, shape: list[str],
                 rule: str = "half", margin: int = 3) -> list[str]:
    """Pull the neighbourhood of a hit back out of pi so you can look at it."""
    height = len(shape)
    span = len(shape[0])
    col = offset % width
    row0 = offset // width
    top = max(0, row0 - margin)
    left = max(0, col - margin)
    right = min(width, col + span + margin)
    bottom = row0 + height + margin
    bits = tape.raw(0, min(len(tape), (bottom + 1) * width)).translate(
        BITS_RULES[rule])
    out = []
    for r in range(top, bottom):
        start = r * width
        if start >= len(bits):
            break
        line = bits[start + left:start + right].decode("ascii", "replace")
        inside = (row0 <= r < row0 + height)
        out.append((line, inside, col - left, span))
    return out
