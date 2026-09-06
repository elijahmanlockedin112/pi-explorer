"""Hand-rolled statistics. No scipy in a zero-dependency toy."""

from __future__ import annotations

import math

__all__ = [
    "normal_sf", "normal_two_sided", "gamma_q", "gamma_p", "chi2_sf",
    "poisson_two_sided", "poisson_at_least_one", "chi2_of_counts",
    "expected_hits", "prob_appears", "expected_first_position",
]

_SQRT2 = math.sqrt(2.0)


def normal_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / _SQRT2)


def normal_two_sided(z: float) -> float:
    """P(|Z| > |z|)."""
    return math.erfc(abs(z) / _SQRT2)


def _gser(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a,x), by series. Best for x < a+1."""
    if x <= 0:
        return 0.0
    ap = a
    total = delta = 1.0 / a
    for _ in range(100_000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-16:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a,x), by continued fraction."""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b if b != 0 else 1.0 / tiny
    h = d
    for i in range(1, 100_000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if a <= 0 or x < 0:
        return float("nan")
    if x == 0:
        return 1.0
    if x < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gser(a, x)))
    return max(0.0, min(1.0, _gcf(a, x)))


def gamma_p(a: float, x: float) -> float:
    return 1.0 - gamma_q(a, x)


def chi2_sf(chi2: float, df: int) -> float:
    """P(X^2 > chi2). Wilson-Hilferty takes over at absurd degrees of freedom."""
    if df <= 0 or chi2 < 0:
        return float("nan")

    def wilson_hilferty() -> float:
        t = (chi2 / df) ** (1.0 / 3.0)
        mu = 1.0 - 2.0 / (9.0 * df)
        sd = math.sqrt(2.0 / (9.0 * df))
        return normal_sf((t - mu) / sd)

    if df > 200_000:
        return wilson_hilferty()
    try:
        value = gamma_q(df / 2.0, chi2 / 2.0)
        if value != value:
            raise ValueError
        return value
    except (ValueError, OverflowError):
        return wilson_hilferty()


def poisson_at_least_one(lam: float) -> float:
    """P(X >= 1) for Poisson(lam) -- 'will it show up at all?'"""
    return -math.expm1(-lam)


def poisson_two_sided(k: int, lam: float) -> float:
    """How surprising is seeing k events when lam were expected?"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k <= 0:
        return min(1.0, 2.0 * math.exp(-lam))
    p_ge = gamma_p(k, lam)        # P(X >= k)
    p_le = gamma_q(k + 1.0, lam)  # P(X <= k)
    return min(1.0, 2.0 * min(p_ge, p_le))


def chi2_of_counts(counts, expected_each: float):
    """Return (chi2, df, p) for observed counts against a flat expectation."""
    values = list(counts.values()) if isinstance(counts, dict) else list(counts)
    if expected_each <= 0 or not values:
        return float("nan"), 0, float("nan")
    chi2 = sum((c - expected_each) ** 2 for c in values) / expected_each
    df = len(values) - 1
    return chi2, df, chi2_sf(chi2, df)


# --- the odds a searcher actually cares about -------------------------------

def expected_hits(n_digits: int, needle_len: int) -> float:
    """How many times an L-digit string should appear in N digits."""
    windows = max(0, n_digits - needle_len + 1)
    return windows / (10.0 ** needle_len)


def prob_appears(n_digits: int, needle_len: int) -> float:
    """P(a given L-digit string appears at least once in N digits).

    Overlapping windows are not independent, but for needles short relative to
    N the Poisson approximation is excellent -- and it is the number everyone
    means when they say '1 in a million odds'.
    """
    return poisson_at_least_one(expected_hits(n_digits, needle_len))


def expected_first_position(needle_len: int) -> float:
    """Roughly how deep you must dig before an L-digit string turns up."""
    return 10.0 ** needle_len
