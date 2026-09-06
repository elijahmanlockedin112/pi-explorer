"""The digit factory.

Chudnovsky's series by binary splitting, spread across every core the machine
will give us, with the digit ceiling chosen from how much RAM is actually free.

    pi = 426880 * sqrt(10005) * Q / T

where P, Q and T come out of a divide-and-conquer walk over the series terms.
Binary splitting keeps every intermediate an exact integer, so the whole
computation is a handful of enormous multiplications and exactly one division.
"""

from __future__ import annotations

import json
import math
import mmap
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .ui import C, commas, human_time, note

__all__ = [
    "MachineProfile", "profile_machine", "compute_pi", "Tape", "ensure_tape",
    "vault_meta", "vault_path", "write_vault", "PI_TRUTH_100", "DEFAULT_DIGITS",
    "max_safe_digits", "estimate_seconds", "VAULT",
]

# Chudnovsky gains ~14.18 decimal digits per term. This constant is why a
# laptop can do a million digits before you finish reading this docstring.
DIGITS_PER_TERM = 14.181647462725477
C3_OVER_24 = 640320 ** 3 // 24

VAULT = Path(os.environ.get("PI_EXPLORER_HOME", Path.home() / ".pi_explorer"))
DEFAULT_DIGITS = 100_000

# Peak resident bytes per digit, measured empirically across the whole
# pipeline (splitting, isqrt, the big divide, decimal rendering). Generous on
# purpose: running the machine out of memory mid-compute is a miserable way to
# find out you were optimistic.
BYTES_PER_DIGIT = 42

# Below this, process startup costs more than the work saved.
PARALLEL_THRESHOLD = 40_000

# The opening of pi, so we can tell "we computed pi" from "we computed a very
# confident wrong number".
PI_TRUTH_100 = ("1415926535897932384626433832795028841971693993751"
                "0582097494459230781640628620899862803482534211706")


# ---------------------------------------------------------------------------
# what are we running on?
# ---------------------------------------------------------------------------

class MachineProfile:
    def __init__(self, cores: int, threads: int, ram_total: int, ram_free: int):
        self.cores = cores
        self.threads = threads
        self.ram_total = ram_total
        self.ram_free = ram_free

    @property
    def workers(self) -> int:
        # Leave one thread for the OS and the terminal so the machine stays
        # usable while it grinds.
        return max(1, self.threads - 1)

    def describe(self) -> str:
        gb = lambda b: f"{b/(1024**3):.1f} GB"
        return (f"{self.threads} logical cores  |  {gb(self.ram_total)} RAM "
                f"({gb(self.ram_free)} free)")


def _memory_bytes() -> tuple[int, int]:
    """(total, available) physical memory in bytes; best effort per platform."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except Exception:
            pass
    else:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            avail = total
            meminfo = Path("/proc/meminfo")
            if meminfo.exists():
                for line in meminfo.read_text().splitlines():
                    if line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) * 1024
                        break
            return total, avail
        except Exception:
            pass
    # Unknown machine: assume a modest 4 GB so we never over-promise.
    return 4 * 1024 ** 3, 2 * 1024 ** 3


_PROFILE: MachineProfile | None = None


def profile_machine(refresh: bool = False) -> MachineProfile:
    global _PROFILE
    if _PROFILE is None or refresh:
        threads = os.cpu_count() or 1
        try:
            cores = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        except AttributeError:
            cores = threads
        total, avail = _memory_bytes()
        _PROFILE = MachineProfile(cores, threads, total, avail)
    return _PROFILE


def max_safe_digits(profile: MachineProfile | None = None,
                    headroom: float = 0.70) -> int:
    """Largest digit count this machine can attempt without swapping itself
    into a coma. Uses free RAM, not total, because your browser is open."""
    profile = profile or profile_machine()
    budget = profile.ram_free * headroom
    return max(10_000, int(budget / BYTES_PER_DIGIT))


# Reference point for the cost model: seconds to do this many digits. Measured
# on a 16-thread desktop; replaced by your own timings after the first run.
REFERENCE_RUN = (1_000_000, 5.1)
# Big-integer work scales like Karatsuba, not linearly. Doubling the digits
# costs about 2.9x, which is 2**GROWTH.
GROWTH = 1.53


def estimate_seconds(n_digits: int, profile: MachineProfile | None = None) -> float:
    """Wall-clock estimate, self-calibrating against your last real run."""
    ref_n, ref_t = REFERENCE_RUN
    meta = vault_meta()
    if meta.get("seconds", 0) > 0.2 and meta.get("digits", 0) >= 100_000:
        ref_n, ref_t = meta["digits"], meta["seconds"]
    return ref_t * (max(1, n_digits) / ref_n) ** GROWTH


def suggested_digits(profile: MachineProfile | None = None,
                     patience: float = 30.0) -> int:
    """The biggest round number this machine can do in `patience` seconds
    without eating all the RAM. What `--auto` picks for you."""
    ceiling = max_safe_digits(profile)
    for candidate in (100_000_000, 50_000_000, 20_000_000, 10_000_000,
                      5_000_000, 2_000_000, 1_000_000, 500_000, 250_000,
                      100_000, 50_000):
        if candidate <= ceiling and estimate_seconds(candidate) <= patience:
            return candidate
    return min(ceiling, 25_000)


# ---------------------------------------------------------------------------
# binary splitting
# ---------------------------------------------------------------------------

def _split(a: int, b: int):
    """(P, Q, T) for Chudnovsky terms [a, b)."""
    if b - a == 1:
        if a == 0:
            pab = qab = 1
        else:
            pab = (6 * a - 5) * (2 * a - 1) * (6 * a - 1)
            qab = a * a * a * C3_OVER_24
        tab = pab * (13591409 + 545140134 * a)
        if a & 1:
            tab = -tab
        return pab, qab, tab
    m = (a + b) >> 1
    p1, q1, t1 = _split(a, m)
    p2, q2, t2 = _split(m, b)
    return p1 * p2, q1 * q2, q2 * t1 + p1 * t2


def _combine(left, right):
    """Glue two adjacent (P, Q, T) blocks into one."""
    p1, q1, t1 = left
    p2, q2, t2 = right
    return p1 * p2, q1 * q2, q2 * t1 + p1 * t2


# Module-level so Windows' spawn-based multiprocessing can pickle them.

def _worker_split(bounds):
    sys.setrecursionlimit(100_000)
    return _split(bounds[0], bounds[1])


def _worker_combine(pair):
    return _combine(pair[0], pair[1])


def _worker_isqrt(prec: int):
    """sqrt(10005) scaled up to `prec` digits -- independent of the series, so
    it can run on its own core while the splitting happens."""
    return math.isqrt(10005 * 10 ** (2 * prec))


# ---------------------------------------------------------------------------
# the main event
# ---------------------------------------------------------------------------

def compute_pi(n_digits: int, workers: int | None = None,
               chatty: bool = True) -> str:
    """Return the first `n_digits` digits of pi after the decimal point."""
    sys.set_int_max_str_digits(0)
    sys.setrecursionlimit(100_000)

    profile = profile_machine()
    if workers is None:
        workers = profile.workers
    workers = max(1, int(workers))

    prec = n_digits + 16
    n_terms = int(prec / DIGITS_PER_TERM) + 2
    parallel = workers > 1 and n_digits >= PARALLEL_THRESHOLD

    need = n_digits * BYTES_PER_DIGIT
    if chatty:
        print(f"{C.BOLD}Summoning {commas(n_digits)} digits of pi{C.RESET}")
        print(note(f"  {commas(n_terms)} Chudnovsky terms  |  "
                   f"{'%d workers' % workers if parallel else 'single core'}  |  "
                   f"~{need/(1024**3):.2f} GB peak RAM"))
        if need > profile.ram_free * 0.9:
            print(f"{C.ORANGE}  ! this is close to your free RAM "
                  f"({profile.ram_free/(1024**3):.1f} GB). Expect swapping."
                  f"{C.RESET}")

    timings: dict[str, float] = {}
    t_all = time.perf_counter()

    def stage(label: str):
        if chatty:
            print(f"  {C.GREY}{label:<34}{C.RESET}", end="", flush=True)
        return time.perf_counter()

    def finish(key: str, t0: float):
        dt = time.perf_counter() - t0
        timings[key] = dt
        if chatty:
            print(f"{C.GREEN}{human_time(dt)}{C.RESET}", flush=True)

    if not parallel:
        t = stage("binary splitting the series")
        _p, q, tt = _split(0, n_terms)
        finish("split", t)

        t = stage("extracting sqrt(10005)")
        root = _worker_isqrt(prec)
        finish("sqrt", t)
    else:
        # Enough chunks to keep every worker fed, few enough that the tree of
        # merges stays shallow.
        n_chunks = 1
        while n_chunks < workers * 4 and n_terms // (n_chunks * 2) > 64:
            n_chunks *= 2
        edges = [round(i * n_terms / n_chunks) for i in range(n_chunks + 1)]
        bounds = [(edges[i], edges[i + 1]) for i in range(n_chunks)
                  if edges[i + 1] > edges[i]]

        t = stage(f"splitting across {workers} cores")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            # Kick off the square root first so it owns a core for the whole
            # run instead of waiting in line behind the series.
            root_future = pool.submit(_worker_isqrt, prec)
            parts = list(pool.map(_worker_split, bounds))

            # Merge pairwise, level by level; every level is parallel except
            # the last one, which is a single enormous multiply.
            while len(parts) > 2:
                pairs = [(parts[i], parts[i + 1])
                         for i in range(0, len(parts) - 1, 2)]
                leftover = [parts[-1]] if len(parts) % 2 else []
                parts = list(pool.map(_worker_combine, pairs)) + leftover
            finish("split", t)

            t = stage("collecting sqrt(10005)")
            root = root_future.result()
            finish("sqrt", t)

        t = stage("final merge")
        _p, q, tt = parts[0] if len(parts) == 1 else _combine(parts[0], parts[1])
        finish("merge", t)

    t = stage("one enormous division")
    pi_scaled = (q * 426880 * root) // tt
    finish("divide", t)

    t = stage("rendering to decimal")
    text = str(pi_scaled)
    finish("render", t)

    digits = text[1:1 + n_digits]

    if chatty:
        total = time.perf_counter() - t_all
        print(f"  {C.CYAN}{C.BOLD}{commas(n_digits)} digits in "
              f"{human_time(total)}{C.RESET} "
              f"{C.GREY}({commas(int(n_digits/max(total, 1e-9)))} digits/sec)"
              f"{C.RESET}")
    return digits


# ---------------------------------------------------------------------------
# the vault: where the digits live between runs
# ---------------------------------------------------------------------------

class Tape:
    """A memory-mapped ribbon of pi.

    mmap is the whole trick behind 'ridiculous amount of digits': the OS pages
    in only the bytes a search actually touches, so a 2 GB digit file opens
    instantly and `find` still runs at C speed over all of it.
    """

    def __init__(self, path: Path, meta: dict | None = None):
        self.path = Path(path)
        self.meta = meta or {}
        self._fh = open(self.path, "rb")
        self.mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def __len__(self) -> int:
        return len(self.mm)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        try:
            self.mm.close()
            self._fh.close()
        except Exception:
            pass

    def get(self, start: int, length: int) -> str:
        start = max(0, start)
        return self.mm[start:start + length].decode("ascii", "replace")

    def raw(self, start: int = 0, length: int | None = None) -> bytes:
        end = len(self.mm) if length is None else start + length
        return self.mm[start:min(end, len(self.mm))]

    def find(self, needle: str | bytes, start: int = 0,
             end: int | None = None) -> int:
        if isinstance(needle, str):
            needle = needle.encode()
        return self.mm.find(needle, start, len(self.mm) if end is None else end)


def vault_path() -> Path:
    return VAULT / "pi.dat"


def vault_meta() -> dict:
    path = VAULT / "pi.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    if vault_path().exists():
        return {"digits": vault_path().stat().st_size, "source": "unknown"}
    return {"digits": 0}


def write_vault(digits: str, source: str, seconds: float) -> None:
    VAULT.mkdir(parents=True, exist_ok=True)
    tmp = VAULT / "pi.dat.tmp"
    tmp.write_bytes(digits.encode("ascii"))
    tmp.replace(vault_path())
    (VAULT / "pi.json").write_text(json.dumps({
        "digits": len(digits),
        "source": source,
        "seconds": round(seconds, 3),
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": profile_machine().describe(),
    }, indent=2))


def verify_digits(digits: str) -> bool:
    head = PI_TRUTH_100[:min(100, len(digits))]
    return digits.startswith(head)


def ensure_tape(want: int | None = None, workers: int | None = None,
                chatty: bool = True) -> Tape:
    """Open the vault, computing more digits first if we are short."""
    meta = vault_meta()
    have = meta.get("digits", 0)
    want = want or have or DEFAULT_DIGITS

    if not vault_path().exists() or have < want:
        if chatty and have:
            print(note(f"vault holds {commas(have)} digits, you want "
                       f"{commas(want)} -- topping up."))
        ceiling = max_safe_digits()
        if want > ceiling:
            raise SystemExit(
                f"{C.RED}{commas(want)} digits needs about "
                f"{want*BYTES_PER_DIGIT/(1024**3):.1f} GB of RAM; this machine "
                f"can safely do about {commas(ceiling)}.{C.RESET}")
        t0 = time.perf_counter()
        digits = compute_pi(max(want, DEFAULT_DIGITS), workers=workers,
                            chatty=chatty)
        if not verify_digits(digits):
            raise SystemExit(f"{C.RED}Integrity check failed -- "
                             f"that is not pi.{C.RESET}")
        write_vault(digits, "chudnovsky", time.perf_counter() - t0)

    return Tape(vault_path(), vault_meta())


def window(tape: Tape, requested: int | None) -> int:
    """How many digits of the tape a command should actually look at."""
    if not requested or requested <= 0:
        return len(tape)
    return min(requested, len(tape))
