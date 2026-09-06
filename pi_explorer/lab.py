"""The randomness lab.

Nobody has ever proved pi is normal -- that every digit and every block of
digits shows up exactly as often as chance says it should. This module runs
the standard battery of randomness tests on pi's digits and, crucially, runs
the *same* battery on a real cryptographic random stream so you can see
whether pi looks any weirder than actual randomness.

Spoiler: it doesn't. Nobody has ever found where it breaks.
"""

from __future__ import annotations

import math
import os
import random
import zlib
from dataclasses import dataclass

from .stats import chi2_of_counts, chi2_sf, normal_two_sided

__all__ = ["TestResult", "battery", "synthesize", "pair_matrix", "SOURCES"]

_TO_VALUES = bytes.maketrans(b"0123456789", bytes(range(10)))
_TO_HILO = bytes.maketrans(b"0123456789", b"LLLLLHHHHH")

SOURCES = {
    "pi": "the digits of pi",
    "mt": "Python's Mersenne Twister",
    "os": "the OS cryptographic RNG",
}


@dataclass
class TestResult:
    key: str
    name: str
    detail: str
    p: float | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# control groups
# ---------------------------------------------------------------------------

def synthesize(kind: str, n: int, seed: int | None = None) -> bytes:
    """Manufacture n ASCII digits from a known-good random source."""
    if kind == "mt":
        rng = random.Random(seed if seed is not None else 0x31415926)
        return bytes(rng.choices(b"0123456789", k=n))
    if kind == "os":
        out = bytearray()
        while len(out) < n:
            need = n - len(out)
            buf = os.urandom(int(need * 1.15) + 64)
            # 250 = 25*10, so rejecting >=250 keeps the digits perfectly uniform.
            out.extend(48 + (b % 10) for b in buf if b < 250)
        return bytes(out[:n])
    raise ValueError(f"unknown source {kind!r}")


# ---------------------------------------------------------------------------
# individual tests
# ---------------------------------------------------------------------------

def _kgram_counts(values: bytes, k: int) -> list[int]:
    """Counts of every k-digit block, over sliding (overlapping) windows."""
    n = len(values)
    counts = [0] * (10 ** k)
    if n < k:
        return counts
    modulus = 10 ** (k - 1)
    code = 0
    for i in range(k):
        code = code * 10 + values[i]
    counts[code] += 1
    for i in range(k, n):
        code = (code % modulus) * 10 + values[i]
        counts[code] += 1
    return counts


def _test_frequency(values: bytes) -> tuple[TestResult, list[int]]:
    counts = [values.count(d) for d in range(10)]
    n = len(values)
    chi2, df, p = chi2_of_counts(counts, n / 10.0)
    entropy = 0.0
    for c in counts:
        if c:
            q = c / n
            entropy -= q * math.log2(q)
    return TestResult(
        "freq", "digit frequency",
        f"chi2={chi2:.2f} df={df}", p,
        f"entropy {entropy:.6f} bits/digit (max {math.log2(10):.6f})",
    ), counts


def _test_blocks(values: bytes, k: int, counts: list[int]) -> TestResult | None:
    cells = 10 ** k
    windows = len(values) - k + 1
    expected = windows / cells
    if expected < 5:
        return None
    chi2, df, p = chi2_of_counts(counts, expected)
    return TestResult(
        f"block{k}", f"{k}-digit blocks",
        f"chi2={chi2:,.0f} df={df:,}", p,
        f"{cells:,} possible blocks, {expected:,.1f} expected each",
    )


def _test_coverage(values: bytes, k: int, counts: list[int]) -> TestResult | None:
    """How many of the 10^k blocks show up at all -- the coupon collector.

    Only interesting where a decent number are expected to be missing; if
    every block is bound to appear, 'they all appeared' proves nothing.
    """
    cells = 10 ** k
    windows = len(values) - k + 1
    if cells * math.exp(-windows / cells) < 5:
        return None
    seen = sum(1 for c in counts if c)
    expected = cells * (1.0 - (1.0 - 1.0 / cells) ** windows)
    variance = max(1e-9, cells * ((1 - 1 / cells) ** windows)
                   * (1 - (1 - 1 / cells) ** windows))
    z = (seen - expected) / math.sqrt(variance)
    missing = cells - seen
    smallest = next((i for i, c in enumerate(counts) if not c), None)
    note = f"{missing:,} never appear"
    if smallest is not None:
        note += f"; smallest missing is {str(smallest).zfill(k)}"
    return TestResult(
        f"cover{k}", f"{k}-block coverage",
        f"{seen:,} of {cells:,} seen (expected {expected:,.0f})",
        normal_two_sided(z), note,
    )


def _test_poker(values: bytes) -> TestResult | None:
    """Deal pi into 5-digit hands and count how many distinct digits each has."""
    hands = len(values) // 5
    if hands < 500:
        return None
    buckets = [0] * 6
    for i in range(0, hands * 5, 5):
        buckets[len(set(values[i:i + 5]))] += 1
    # P(exactly r distinct) = C(10,r) * S(5,r) * r! / 10^5
    probs = [0.0, 0.0001, 0.0135, 0.18, 0.504, 0.3024]
    chi2 = 0.0
    used = 0
    for r in range(1, 6):
        expected = hands * probs[r]
        if expected < 5:
            continue
        chi2 += (buckets[r] - expected) ** 2 / expected
        used += 1
    df = used - 1
    labels = {1: "five of a kind", 2: "four/full house", 3: "three of a kind",
              4: "one pair", 5: "all different"}
    detail = f"chi2={chi2:.2f} df={df}"
    note = ", ".join(f"{labels[r]}: {buckets[r]:,}" for r in (5, 4, 3, 2))
    return TestResult("poker", f"poker hands ({hands:,})", detail,
                      chi2_sf(chi2, df), note)


def _test_gaps(values: bytes, max_gap: int = 40) -> TestResult:
    """Distance between repeats of the same digit should be geometric(0.1)."""
    bins = [0] * (max_gap + 1)
    last = [-1] * 10
    for i, v in enumerate(values):
        prev = last[v]
        if prev >= 0:
            bins[min(i - prev - 1, max_gap)] += 1
        last[v] = i
    total = sum(bins)
    chi2 = 0.0
    used = 0
    for g in range(max_gap):
        expected = total * (0.9 ** g) * 0.1
        if expected < 5:
            continue
        chi2 += (bins[g] - expected) ** 2 / expected
        used += 1
    tail_expected = total * (0.9 ** max_gap)
    if tail_expected >= 5:
        chi2 += (bins[max_gap] - tail_expected) ** 2 / tail_expected
        used += 1
    df = used - 1
    return TestResult("gaps", "gaps between repeats", f"chi2={chi2:.2f} df={df}",
                      chi2_sf(chi2, df), f"{total:,} gaps measured")


def _test_serial(values: bytes) -> TestResult:
    """Lag-1 correlation. If digit n told you anything about digit n+1, this
    is where it would show up."""
    n = len(values) - 1
    a, b = values[:-1], values[1:]
    sx, sy = sum(a), sum(b)
    sxy = sum(p * q for p, q in zip(a, b))
    sxx = sum(p * p for p in a)
    syy = sum(q * q for q in b)
    denom = math.sqrt(max(1e-9, (n * sxx - sx * sx) * (n * syy - sy * sy)))
    r = (n * sxy - sx * sy) / denom
    z = r * math.sqrt(n)
    return TestResult("serial", "serial correlation", f"r={r:+.6f} z={z:+.2f}",
                      normal_two_sided(z), "0.000000 would be perfect")


def _test_runs(data: bytes) -> TestResult:
    """Wald-Wolfowitz runs test on high (5-9) versus low (0-4) digits."""
    hilo = data.translate(_TO_HILO)
    n = len(hilo)
    n1 = hilo.count(72)  # 'H'
    n2 = n - n1
    runs = 1 + sum(1 for x, y in zip(hilo, hilo[1:]) if x != y)
    mean = 2.0 * n1 * n2 / n + 1.0
    var = (2.0 * n1 * n2 * (2.0 * n1 * n2 - n)) / (n * n * (n - 1.0))
    z = (runs - mean) / math.sqrt(max(var, 1e-9))
    return TestResult("runs", "high/low runs", f"{runs:,} runs, z={z:+.2f}",
                      normal_two_sided(z), f"expected {mean:,.0f}")


def _test_walk(values: bytes) -> TestResult:
    """Treat digits as steps and see how far the walk drifts from the origin."""
    n = len(values)
    drift = sum(values) - 4.5 * n
    sigma = math.sqrt(n * 8.25)  # variance of a uniform digit is 99/12
    z = drift / sigma
    return TestResult("walk", "random walk drift", f"drift={drift:+,.0f} z={z:+.2f}",
                      normal_two_sided(z), f"one sigma is {sigma:,.0f}")


def _test_compression(data: bytes) -> TestResult:
    """If the digits held a pattern, a compressor would find it and shrink
    them. Truly random digits cost log2(10) = 3.3219 bits each; zlib's Huffman
    coding lands just above that and cannot do better without a real pattern
    to exploit."""
    packed = zlib.compress(data, 9)
    bits = 8.0 * len(packed) / len(data)
    return TestResult("zlib", "compressibility", f"{bits:.4f} bits/digit", None,
                      f"floor is {math.log2(10):.4f}; "
                      f"{len(data):,} -> {len(packed):,} bytes")


# ---------------------------------------------------------------------------
# the whole battery
# ---------------------------------------------------------------------------

def battery(data: bytes, deep: bool = False) -> tuple[list[TestResult], list[int]]:
    """Run every test on a stream of ASCII digits."""
    values = data.translate(_TO_VALUES)
    n = len(values)
    results: list[TestResult] = []

    freq, counts = _test_frequency(values)
    results.append(freq)

    # Block sizes worth testing: enough expected hits per block for chi-square
    # to mean anything, and small enough that the counter array fits in RAM.
    block_ks = [k for k in range(2, (7 if deep else 5))
                if (n - k + 1) / 10 ** k >= 5]
    # Coverage is only informative where some blocks should still be missing,
    # which is always one or two sizes above the block tests.
    cover_ks = [k for k in range(3, 9)
                if 10 ** k <= 10_000_000
                and 10 ** k * math.exp(-(n - k + 1) / 10 ** k) >= 5]
    cover_ks = cover_ks[:2] if deep else cover_ks[:1]

    for k in sorted(set(block_ks) | set(cover_ks)):
        counts_k = _kgram_counts(values, k)
        if k in block_ks:
            block = _test_blocks(values, k, counts_k)
            if block:
                results.append(block)
        if k in cover_ks:
            cover = _test_coverage(values, k, counts_k)
            if cover:
                results.append(cover)
        del counts_k

    poker = _test_poker(values)
    if poker:
        results.append(poker)

    results.append(_test_gaps(values))
    results.append(_test_serial(values))
    results.append(_test_runs(data))
    results.append(_test_walk(values))
    results.append(_test_compression(data))
    return results, counts


def pair_matrix(data: bytes) -> list[list[int]]:
    """10x10 counts of which digit follows which."""
    values = data.translate(_TO_VALUES)
    grid = [[0] * 10 for _ in range(10)]
    for a, b in zip(values, values[1:]):
        grid[a][b] += 1
    return grid
