"""The digit factory.

Chudnovsky's series by binary splitting, spread across every core the machine
will give us, with the digit ceiling chosen from how much RAM is actually free.

    pi = 426880 * sqrt(10005) * Q / T

where P, Q and T come out of a divide-and-conquer walk over the series terms.
Binary splitting keeps every intermediate an exact integer, so the whole
computation is a handful of enormous multiplications and exactly one division.
"""

from __future__ import annotations

import gc
import json
import math
import mmap
import os
import sys
import time
import weakref
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .ui import C, commas, human_time, note

__all__ = [
    "MachineProfile", "profile_machine", "compute_pi", "Tape", "ensure_tape",
    "vault_meta", "vault_path", "write_vault", "PI_TRUTH_100", "DEFAULT_DIGITS",
    "max_safe_digits", "estimate_seconds", "VAULT", "plan_workers",
    "pmul_many", "DIGITS_PER_WORKER",
]

# Chudnovsky gains ~14.18 decimal digits per term. This constant is why a
# laptop can do a million digits before you finish reading this docstring.
DIGITS_PER_TERM = 14.181647462725477
C3_OVER_24 = 640320 ** 3 // 24

VAULT = Path(os.environ.get("PI_EXPLORER_HOME", Path.home() / ".pi_explorer"))
DEFAULT_DIGITS = 100_000

# Peak resident bytes per digit. Measured: 33 in the parent at 2M digits, 25
# at 4M, plus roughly a third again spread across the workers holding limbs.
# Rounded well up on purpose -- running the machine out of memory halfway
# through a long compute is a miserable way to learn you were optimistic.
BYTES_PER_DIGIT = 56

# Below this, process startup costs more than the work saved.
PARALLEL_THRESHOLD = 300_000

# Spawning a worker on Windows costs the best part of a tenth of a second, so
# the pool is sized to the job rather than to the machine. Measured optimum on
# a 16-thread desktop: ~4 workers at 500k digits, ~8 at 1M, all of them past
# 2M. Below the threshold a single core genuinely wins -- at 100,000 digits,
# fifteen workers are eleven times slower than none.
DIGITS_PER_WORKER = 125_000

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
# on a 16-thread desktop (1M in 2.8s, 2M in 7.8s, 4M in 23.3s); replaced by
# your own timings after the first real run.
REFERENCE_RUN = (1_000_000, 2.8)
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


def plan_workers(n_digits: int, workers: int | None = None) -> int:
    """How many processes this job should actually use.

    Use the whole machine, but only as much of it as the job can keep busy.
    Past that point the spawn cost is pure loss: fifteen workers on a hundred
    thousand digits are eleven times slower than one.
    """
    if workers is not None:
        return max(1, int(workers))
    return max(1, min(profile_machine().workers,
                      n_digits // DIGITS_PER_WORKER))


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


def _worker_mul(pair):
    return pair[0] * pair[1]


# ---------------------------------------------------------------------------
# parallel big-integer multiplication
# ---------------------------------------------------------------------------
#
# Binary splitting parallelises beautifully at the bottom of the tree and not
# at all at the top: the last few merges are one or two enormous multiplies,
# and that is where a 16-core machine sits at 1/16 utilisation.
#
# The fix is to parallelise the multiplication itself. Cut each operand into k
# limbs and the product becomes k*k independent limb products that reassemble
# with shifts and adds:
#
#     a = SUM a_i * 2^(i*sa)      b = SUM b_j * 2^(j*sb)
#     a*b = SUM_ij (a_i * b_j) * 2^(i*sa + j*sb)
#
# It is more total work than one Karatsuba multiply -- each limb product is
# cheaper than 1/k^2 of the whole -- but it is spread over k^2 cores, and
# wall-clock is what we are buying. Measured 3.1x on 2M-digit operands.

# Below this, the process round trip costs more than the multiply saves.
PARALLEL_MUL_BITS = 400_000


def _limbs(value: int, k: int) -> tuple[list[int], int]:
    shift = (value.bit_length() + k - 1) // k
    mask = (1 << shift) - 1
    return [(value >> (i * shift)) & mask for i in range(k)], shift


def _limb_split(workers: int, pairs: int) -> int:
    """How finely to cut operands so every core has something to chew on.

    Splitting into k limbs costs about k^0.4 extra total work, so we only cut
    as far as there are cores to absorb it. Past k=3 the extra work outweighs
    the extra parallelism -- measured, not guessed.
    """
    k = 1
    while k < 3 and pairs * (k + 1) ** 2 <= workers * 1.5:
        k += 1
    return k


def pmul_many(pairs: list[tuple[int, int]], pool, workers: int) -> list[int]:
    """Multiply a batch of pairs, spreading the big ones over the pool.

    Batching matters: submitting every limb product from every pair in one go
    keeps all the workers busy instead of draining the pool between products.
    """
    if pool is None or not pairs:
        return [a * b for a, b in pairs]

    k = _limb_split(workers, len(pairs))
    jobs: list[tuple[int, int]] = []
    plans: list[tuple] = []
    for a, b in pairs:
        sign = 1
        if a < 0:
            a, sign = -a, -sign
        if b < 0:
            b, sign = -b, -sign
        if min(a.bit_length(), b.bit_length()) < PARALLEL_MUL_BITS:
            plans.append((False, sign, a, b))
            continue
        # k == 1 still goes to the pool: one whole product per worker is the
        # right shape when there are already more products than cores.
        limbs_a, shift_a = _limbs(a, k)
        limbs_b, shift_b = _limbs(b, k)
        plans.append((True, sign, len(jobs), k, shift_a, shift_b))
        jobs.extend((x, y) for x in limbs_a for y in limbs_b)

    products = list(pool.map(_worker_mul, jobs)) if jobs else []

    out: list[int] = []
    for plan in plans:
        if not plan[0]:
            _, sign, a, b = plan
            out.append(sign * (a * b))
            continue
        _, sign, start, kk, shift_a, shift_b = plan
        total = 0
        index = start
        for i in range(kk):
            for j in range(kk):
                total += products[index] << (i * shift_a + j * shift_b)
                index += 1
        out.append(sign * total)
    return out


def _merge_level(parts: list, pool, workers: int, need_p: bool = True) -> list:
    """One level of the merge tree, with every multiplication parallelised.

    The top of the tree is only one or two merges, so the parallelism has to
    come from inside them: four independent products per merge, each cut into
    limbs. On the very last merge P is dead weight -- nothing will ever be
    combined with the result -- so we skip it.
    """
    merges = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
    leftover = [parts[-1]] if len(parts) % 2 else []

    jobs: list[tuple[int, int]] = []
    for (p1, q1, t1), (p2, q2, t2) in merges:
        if need_p:
            jobs.append((p1, p2))
        jobs.extend([(q1, q2), (q2, t1), (p1, t2)])

    products = pmul_many(jobs, pool, workers)

    merged = []
    stride = 4 if need_p else 3
    for index in range(len(merges)):
        base = index * stride
        if need_p:
            p, qq, qt, pt = products[base:base + 4]
        else:
            p = 0
            qq, qt, pt = products[base:base + 3]
        merged.append((p, qq, qt + pt))
    return merged + leftover


def _trim_ratio(q: int, t: int, prec: int) -> tuple[int, int]:
    """Throw away the low-order bits of Q and T before the final division.

    Q and T come out of the series with roughly twice as many digits as the
    answer needs -- for a million digits of pi they run to nearly two million
    each. Only the *ratio* matters, so shifting both down by the same amount
    leaves the quotient alone to well past the precision we are keeping, and
    the final multiply and divide shrink accordingly. This is the single
    largest saving in the whole pipeline.
    """
    keep = int(prec * 3.3219280948873626) + 256   # target bits + fat guard
    shift = min(q.bit_length(), t.bit_length()) - keep
    if shift <= 0:
        return q, t
    return q >> shift, t >> shift


# ---------------------------------------------------------------------------
# the main event
# ---------------------------------------------------------------------------

def compute_pi(n_digits: int, workers: int | None = None,
               chatty: bool = True, on_stage=None) -> str:
    """Return the first `n_digits` digits of pi after the decimal point.

    `on_stage(label, seconds)` is called as each phase begins (seconds None)
    and ends, so a UI can show what the machine is doing right now.
    """
    sys.set_int_max_str_digits(0)
    sys.setrecursionlimit(100_000)

    profile = profile_machine()
    asked = workers
    workers = plan_workers(n_digits, workers)

    prec = n_digits + 16
    n_terms = int(prec / DIGITS_PER_TERM) + 2
    parallel = workers > 1 and (n_digits >= PARALLEL_THRESHOLD or asked)

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

    current = {"label": ""}

    def stage(label: str):
        current["label"] = label
        if chatty:
            print(f"  {C.GREY}{label:<34}{C.RESET}", end="", flush=True)
        if on_stage:
            on_stage(label, None)
        return time.perf_counter()

    def finish(key: str, t0: float):
        dt = time.perf_counter() - t0
        timings[key] = dt
        if chatty:
            print(f"{C.GREEN}{human_time(dt)}{C.RESET}", flush=True)
        if on_stage:
            on_stage(current["label"], dt)

    if not parallel:
        t = stage("binary splitting the series")
        _p, q, tt = _split(0, n_terms)
        finish("split", t)

        t = stage("extracting sqrt(10005)")
        root = _worker_isqrt(prec)
        finish("sqrt", t)

        t = stage("trimming to working precision")
        q, tt = _trim_ratio(q, tt, prec)
        finish("trim", t)

        t = stage("assembling the numerator")
        numerator = q * 426880 * root
        finish("multiply", t)
    else:
        # Enough leaf blocks to keep every worker fed, few enough that the
        # tree of merges stays shallow.
        n_chunks = 1
        while n_chunks < workers * 4 and n_terms // (n_chunks * 2) > 64:
            n_chunks *= 2
        edges = [round(i * n_terms / n_chunks) for i in range(n_chunks + 1)]
        bounds = [(edges[i], edges[i + 1]) for i in range(n_chunks)
                  if edges[i + 1] > edges[i]]

        with ProcessPoolExecutor(max_workers=workers) as pool:
            # Kick the square root off first so it owns a core for the whole
            # run instead of queueing behind the series.
            root_future = pool.submit(_worker_isqrt, prec)

            t = stage(f"splitting {len(bounds)} blocks on {workers} cores")
            parts = list(pool.map(_worker_split, bounds))
            finish("split", t)

            # While there are plenty of merges to go round, one merge per core
            # is the efficient shape.
            t = stage("merging the tree")
            while len(parts) > 4:
                pairs = [(parts[i], parts[i + 1])
                         for i in range(0, len(parts) - 1, 2)]
                leftover = [parts[-1]] if len(parts) % 2 else []
                parts = list(pool.map(_worker_combine, pairs)) + leftover
            # Near the top there are too few merges to fill the machine, so
            # the parallelism moves inside each multiplication instead.
            while len(parts) > 1:
                parts = _merge_level(parts, pool, workers,
                                     need_p=len(parts) > 2)
            _p, q, tt = parts[0]
            finish("merge", t)

            t = stage("collecting sqrt(10005)")
            root = root_future.result()
            finish("sqrt", t)

            t = stage("trimming to working precision")
            q, tt = _trim_ratio(q, tt, prec)
            finish("trim", t)

            t = stage("assembling the numerator")
            numerator = pmul_many([(q * 426880, root)], pool, workers)[0]
            finish("multiply", t)

    t = stage("one enormous division")
    pi_scaled = numerator // tt
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

# Every live Tape, weakly held. Rewriting the vault has to unmap the old file
# first (Windows will not replace a mapped file), and the writer has no way to
# know which part of the program is holding it, so the tapes register here.
_OPEN_TAPES: "weakref.WeakSet[Tape]" = weakref.WeakSet()


def _close_open_tapes() -> int:
    """Unmap every open tape. They are about to be stale anyway."""
    tapes = list(_OPEN_TAPES)
    for tape in tapes:
        tape.close()
    gc.collect()
    return len(tapes)


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
        _OPEN_TAPES.add(self)

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
        _OPEN_TAPES.discard(self)

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


def _replace_with_retry(src: Path, dest: Path, attempts: int = 10) -> None:
    """Move src onto dest, working around Windows file locking.

    Windows refuses to replace a file while anything still holds it open --
    including one of our own memory maps. Anything that writes the vault is
    supposed to release its Tape first, but a Tape that was merely dropped
    rather than closed keeps the mapping alive until the collector runs, so
    give the collector a nudge and retry before giving up.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == 0:
                gc.collect()          # finalise any Tape that was dropped
            elif attempt >= 3 and dest.exists():
                try:
                    dest.unlink()     # last resort: unlink, then rename
                except OSError:
                    pass
            time.sleep(0.05 * (attempt + 1))
    raise SystemExit(
        f"{C.RED}Could not write {dest}: another process still has the digit "
        f"file open.{C.RESET}\nClose any other pi-explorer session and try "
        f"again, or set PI_EXPLORER_HOME to a different folder.")


def write_vault(digits: str, source: str, seconds: float) -> None:
    VAULT.mkdir(parents=True, exist_ok=True)
    # Unmap the previous digits before touching the file. Skipping this is
    # what produced "WinError 5: Access is denied" when compute followed a
    # search in the same session.
    _close_open_tapes()
    tmp = VAULT / "pi.dat.tmp"
    tmp.write_bytes(digits.encode("ascii"))
    _replace_with_retry(tmp, vault_path())
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
