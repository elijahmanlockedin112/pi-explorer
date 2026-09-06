"""Pattern hunting: the weird stuff hiding in the first few million digits."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .engine import Tape

__all__ = [
    "Finding", "long_runs", "longest_run", "longest_palindrome",
    "self_locating", "staircases", "digit_deserts", "first_occurrences",
    "most_elusive", "never_appears", "FEYNMAN_POINT",
]

# Six 9s in a row, 762 digits in. Feynman said he wanted to memorise pi to
# there so he could recite it, finish "...nine nine nine nine nine nine", and
# say "and so on" -- implying, straight-faced, that pi is rational.
FEYNMAN_POINT = 762


@dataclass
class Finding:
    label: str
    detail: str
    position: int = -1
    extra: str = ""


def _text(tape: Tape, limit: int | None) -> str:
    end = min(limit or len(tape), len(tape))
    return tape.raw(0, end).decode("ascii")


def long_runs(tape: Tape, limit: int | None = None,
              minimum: int = 6) -> list[Finding]:
    """Every place a single digit repeats `minimum` times or more."""
    text = _text(tape, limit)
    out = []
    for match in re.finditer(r"(\d)\1{%d,}" % (minimum - 1), text):
        run = match.group(0)
        out.append(Finding(
            label=f"{len(run)}x{run[0]}",
            detail=run,
            position=match.start() + 1,
        ))
    out.sort(key=lambda f: (-len(f.detail), f.position))
    return out


def longest_run(tape: Tape, limit: int | None = None) -> Finding | None:
    runs = long_runs(tape, limit, minimum=2)
    return runs[0] if runs else None


def longest_palindrome(tape: Tape, limit: int | None = None) -> Finding | None:
    """Manacher's algorithm -- longest palindromic run of digits, in O(n).

    Every position gets a radius, each character is visited a constant number
    of times amortised, and a million digits falls out in a couple of seconds.
    """
    text = _text(tape, limit)
    n = len(text)
    if n < 3:
        return None
    # Interleave with sentinels so even- and odd-length palindromes are one case.
    padded = "^#" + "#".join(text) + "#$"
    radius = [0] * len(padded)
    center = right = 0
    best_len = best_center = 0
    for i in range(1, len(padded) - 1):
        if i < right:
            radius[i] = min(right - i, radius[2 * center - i])
        while padded[i + radius[i] + 1] == padded[i - radius[i] - 1]:
            radius[i] += 1
        if i + radius[i] > right:
            center, right = i, i + radius[i]
        if radius[i] > best_len:
            best_len, best_center = radius[i], i
    start = (best_center - best_len) // 2
    return Finding(
        label=f"{best_len}-digit palindrome",
        detail=text[start:start + best_len],
        position=start + 1,
    )


def self_locating(tape: Tape, limit: int | None = None,
                  cap: int = 12) -> list[Finding]:
    """Numbers that sit at their own address.

    '16470' really does begin at position 16,470. Pi keeping a filing system
    of itself is a coincidence, but it is a very good one.
    """
    end = min(limit or len(tape), len(tape))
    found = []
    for i in range(1, end):
        key = str(i)
        if len(key) > end - (i - 1):
            break
        if tape.get(i - 1, len(key)) == key:
            found.append(Finding(label="self-locating", detail=key, position=i))
            if len(found) >= cap:
                break
    return found


def staircases(tape: Tape, limit: int | None = None) -> list[Finding]:
    """Consecutive ascending or descending digit runs."""
    text = _text(tape, limit)
    out = []
    for name, seq in (("ascending", "0123456789"), ("descending", "9876543210")):
        for length in range(len(seq), 4, -1):
            best = None
            for start in range(len(seq) - length + 1):
                needle = seq[start:start + length]
                at = text.find(needle)
                if at >= 0 and (best is None or at < best[1]):
                    best = (needle, at)
            if best:
                out.append(Finding(label=f"{length} {name}", detail=best[0],
                                   position=best[1] + 1))
                break
    return out


def digit_deserts(tape: Tape, limit: int | None = None) -> list[Finding]:
    """The longest stretch of pi that never once uses a given digit."""
    text = _text(tape, limit)
    out = []
    for digit in "0123456789":
        best_len = best_at = 0
        cursor = 0
        for piece in text.split(digit):
            if len(piece) > best_len:
                best_len, best_at = len(piece), cursor
            cursor += len(piece) + 1
        out.append(Finding(label=f"no {digit}", detail=f"{best_len} digits",
                           position=best_at + 1))
    out.sort(key=lambda f: -int(f.detail.split()[0]))
    return out


def first_occurrences(tape: Tape, k: int = 3,
                      limit: int | None = None) -> list[int]:
    """First position (1-based) of every k-digit string; -1 if never seen."""
    end = min(limit or len(tape), len(tape))
    values = tape.raw(0, end).translate(bytes.maketrans(
        b"0123456789", bytes(range(10))))
    cells = 10 ** k
    first = [-1] * cells
    if len(values) < k:
        return first
    modulus = 10 ** (k - 1)
    code = 0
    for i in range(k):
        code = code * 10 + values[i]
    if first[code] < 0:
        first[code] = 1
    remaining = cells - 1
    for i in range(k, len(values)):
        code = (code % modulus) * 10 + values[i]
        if first[code] < 0:
            first[code] = i - k + 2
            remaining -= 1
            if not remaining:
                break
    return first


def most_elusive(tape: Tape, k: int = 3, limit: int | None = None):
    """The k-digit string that makes you wait longest before showing up."""
    first = first_occurrences(tape, k, limit)
    missing = [i for i, p in enumerate(first) if p < 0]
    latest = max(range(len(first)), key=lambda i: first[i])
    return (str(latest).zfill(k), first[latest],
            [str(i).zfill(k) for i in missing[:5]], len(missing))


def never_appears(tape: Tape, k: int, limit: int | None = None):
    """(count missing, smallest missing) among all k-digit strings."""
    first = first_occurrences(tape, k, limit)
    missing = [i for i, p in enumerate(first) if p < 0]
    return len(missing), (str(missing[0]).zfill(k) if missing else None)
