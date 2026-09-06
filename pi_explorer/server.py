"""A local web front end for the explorer.

Standard library only -- http.server plus one HTML file. Binds to localhost
and nothing else: this is a window onto your own machine, not a service.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .engine import (BYTES_PER_DIGIT, Tape, compute_pi, estimate_seconds,
                     max_safe_digits, plan_workers, profile_machine,
                     suggested_digits, vault_meta, vault_path, verify_digits,
                     write_vault)
from .stats import expected_hits, prob_appears

WEB_ROOT = Path(__file__).parent / "web"

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css",
        ".js": "text/javascript", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}


# ---------------------------------------------------------------------------
# shared state
# ---------------------------------------------------------------------------

class Explorer:
    """Owns the tape and the one background compute job."""

    def __init__(self):
        self._lock = threading.RLock()
        self._tape: Tape | None = None
        self.job: dict = {"state": "idle"}

    def tape(self) -> Tape | None:
        with self._lock:
            # Rewriting the vault unmaps every open tape, so ours may have
            # been closed out from under us by a compute. Reopen on demand.
            if self._tape is not None and self._tape.mm.closed:
                self._tape = None
            if self._tape is None and vault_path().exists():
                self._tape = Tape(vault_path(), vault_meta())
            return self._tape

    def release(self) -> None:
        with self._lock:
            if self._tape is not None:
                self._tape.close()
                self._tape = None

    def digits_held(self) -> int:
        tape = self.tape()
        return len(tape) if tape else 0

    # -- the compute job ----------------------------------------------------

    def start_compute(self, target: int) -> dict:
        with self._lock:
            if self.job.get("state") == "running":
                return {"error": "a computation is already running"}
            ceiling = max_safe_digits()
            if target > ceiling:
                return {"error": f"{target:,} digits needs about "
                                 f"{target*BYTES_PER_DIGIT/(1024**3):.1f} GB; "
                                 f"this machine tops out near {ceiling:,}."}
            self.job = {
                "state": "running", "target": target, "stage": "starting",
                "started": time.time(), "estimate": estimate_seconds(target),
                "workers": plan_workers(target), "stages": [],
            }
        threading.Thread(target=self._run_compute, args=(target,),
                         daemon=True).start()
        return self.status()

    def _run_compute(self, target: int) -> None:
        def on_stage(label, seconds):
            with self._lock:
                self.job["stage"] = label
                if seconds is not None:
                    self.job["stages"].append([label, round(seconds, 3)])

        try:
            # Let go of the mapping first: it frees the memory, and on Windows
            # a mapped file cannot be replaced.
            self.release()
            started = time.perf_counter()
            digits = compute_pi(target, chatty=False, on_stage=on_stage)
            elapsed = time.perf_counter() - started
            if not verify_digits(digits):
                raise RuntimeError("integrity check failed -- that is not pi")
            write_vault(digits, "chudnovsky", elapsed)
            self.release()
            with self._lock:
                self.job.update(state="done", stage="done", elapsed=elapsed,
                                digits=len(digits))
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            with self._lock:
                self.job.update(state="error", error=str(exc))

    def status(self) -> dict:
        profile = profile_machine()
        meta = vault_meta()
        with self._lock:
            job = dict(self.job)
        if job.get("state") == "running":
            job["running_for"] = time.time() - job["started"]
        return {
            "version": __version__,
            "digits": self.digits_held(),
            "meta": meta,
            "machine": {
                "threads": profile.threads,
                "workers": profile.workers,
                "ram_total": profile.ram_total,
                "ram_free": profile.ram_free,
                "description": profile.describe(),
                "ceiling": max_safe_digits(),
                "suggested": suggested_digits(),
            },
            "job": job,
        }


EXPLORER = Explorer()


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------

def _need_tape():
    tape = EXPLORER.tape()
    if tape is None or len(tape) == 0:
        raise ValueError("no digits yet -- compute some first")
    return tape


def _int(params, key, default=0):
    try:
        return int(params.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def _str(params, key, default=""):
    return params.get(key, [default])[0]


def api_status(_params):
    return EXPLORER.status()


def api_compute(params):
    target = _int(params, "digits", 0) or suggested_digits()
    return EXPLORER.start_compute(target)


def api_digits(params):
    tape = _need_tape()
    offset = max(0, _int(params, "offset", 0))
    count = max(1, min(_int(params, "count", 20_000), 2_000_000))
    return {"offset": offset, "digits": tape.get(offset, count),
            "total": len(tape)}


def api_search(params):
    from .search import scan
    tape = _need_tape()
    limit = _int(params, "limit", 0) or len(tape)
    limit = min(limit, len(tape))
    out = []
    for raw in _str(params, "q").split(","):
        needle = re.sub(r"[^0-9]", "", raw)
        if not needle:
            continue
        result = scan(tape, needle, limit=limit, collect=_int(params, "list", 20))
        payload = asdict(result)
        payload.update(
            position=result.position, found=result.found,
            expected=result.expected, odds=result.odds_of_appearing,
            luck=result.luck if result.found else None,
            p_value=result.p_value if result.expected >= 1 else None,
            even_odds_depth=0.693 * 10 ** len(needle),
            context=list(_context(tape, result.first, len(needle)))
            if result.found else None,
        )
        out.append(payload)
    return {"results": out, "searched": limit}


def _context(tape, offset, length, pad=28):
    start = max(0, offset - pad)
    return (tape.get(start, offset - start), tape.get(offset, length),
            tape.get(offset + length, pad))


def api_birthday(params):
    from .search import date_variants, parse_date, scan
    tape = _need_tape()
    limit = min(_int(params, "limit", 0) or len(tape), len(tape))
    year, month, day = parse_date(_str(params, "date"))
    rows = []
    for style, digits in date_variants(year, month, day).items():
        result = scan(tape, digits, limit=limit, collect=1)
        rows.append({"style": style, "digits": digits,
                     "found": result.found, "position": result.position,
                     "count": result.count,
                     "odds": result.odds_of_appearing})
    return {"date": f"{year:04d}-{month:02d}-{day:02d}", "rows": rows,
            "searched": limit}


_LETTER_CACHE: dict[int, str] = {}


def api_text(params):
    from .search import encode_text, letter_stream, scan
    tape = _need_tape()
    mode = _str(params, "mode", "pairs")
    limit = min(_int(params, "limit", 0) or len(tape), len(tape))
    words = [re.sub(r"[^a-z]", "", w.lower())
             for w in _str(params, "words").split(",")]
    words = [w for w in words if w]
    rows = []

    if mode == "pairs":
        book = _LETTER_CACHE.get(limit)
        if book is None:
            book = letter_stream(tape, limit)
            _LETTER_CACHE.clear()
            _LETTER_CACHE[limit] = book
        for word in words:
            at = book.find(word)
            expected = 26 ** len(word)
            rows.append({
                "word": word, "found": at >= 0,
                "letter": at + 1 if at >= 0 else None,
                "position": at * 2 + 1 if at >= 0 else None,
                "count": book.count(word) if at >= 0 else 0,
                "expected_letter": expected,
                "expected_digits": expected * 2,
                "context": [book[max(0, at - 20):at], word,
                            book[at + len(word):at + len(word) + 20]]
                if at >= 0 else None,
                "letters_searched": len(book),
            })
    else:
        for word in words:
            encoded = encode_text(word, mode)
            result = scan(tape, encoded, limit=limit, collect=1)
            rows.append({
                "word": word, "encoded": encoded, "found": result.found,
                "position": result.position, "count": result.count,
                "expected_digits": 10 ** len(encoded),
                "context": list(_context(tape, result.first, len(encoded)))
                if result.found else None,
            })
    return {"mode": mode, "rows": rows, "searched": limit}


def api_odds(params):
    n = _int(params, "digits", 0) or EXPLORER.digits_held() or 1_000_000
    rows = []
    for length in range(1, 17):
        rows.append({
            "length": length,
            "one_in": 10 ** length,
            "expected": expected_hits(n, length),
            "chance": prob_appears(n, length),
            "even_odds": 0.693 * 10 ** length,
        })
    return {"digits": n, "rows": rows}


def api_random(params):
    from .lab import battery, pair_matrix, synthesize
    tape = _need_tape()
    limit = min(_int(params, "limit", 1_000_000) or 1_000_000, len(tape))
    data = tape.raw(0, limit)
    deep = _str(params, "deep") == "1"

    started = time.perf_counter()
    results, counts = battery(data, deep=deep)
    columns = {"pi": [_test_json(r) for r in results]}
    for kind in [k for k in _str(params, "vs", "mt,os").split(",") if k]:
        control, _ = battery(synthesize(kind, limit), deep=deep)
        columns[kind] = [_test_json(r) for r in control]

    scored = [r for r in results if r.p is not None]
    return {
        "searched": limit,
        "seconds": time.perf_counter() - started,
        "counts": counts,
        "expected_each": limit / 10,
        "columns": columns,
        "failures": [r.name for r in scored if r.p < 0.01],
        "scored": len(scored),
        "matrix": pair_matrix(tape.raw(0, min(limit, 200_000)))
        if _str(params, "matrix") == "1" else None,
    }


def _test_json(result):
    return {"key": result.key, "name": result.name, "detail": result.detail,
            "p": result.p, "note": result.note}


def api_hunt(params):
    from . import hunt as H
    tape = _need_tape()
    limit = min(_int(params, "limit", 0) or len(tape), len(tape))
    cap = min(limit, 1_000_000)
    palindrome = H.longest_palindrome(tape, cap)
    out = {
        "searched": limit,
        "feynman": tape.get(H.FEYNMAN_POINT - 1, 6),
        "runs": [{"label": f.label, "detail": f.detail, "position": f.position}
                 for f in H.long_runs(tape, limit, minimum=7)[:8]],
        "palindrome": {"detail": palindrome.detail,
                       "position": palindrome.position} if palindrome else None,
        "self_locating": [{"detail": f.detail, "position": f.position}
                          for f in H.self_locating(tape, cap, cap=8)],
        "staircases": [{"label": f.label, "detail": f.detail,
                        "position": f.position}
                       for f in H.staircases(tape, limit)],
        "deserts": [{"label": f.label, "detail": f.detail,
                     "position": f.position}
                    for f in H.digit_deserts(tape, limit)[:3]],
        "holdouts": [],
    }
    for k in (3, 4, 5):
        if 10 ** k > limit // 5:
            break
        latest, at, examples, missing = H.most_elusive(tape, k, limit)
        out["holdouts"].append({"k": k, "string": latest, "position": at,
                                "missing": missing, "examples": examples})
    return out


def api_shapes(_params):
    from .search import SHAPE_LIBRARY, parse_shape
    return {"shapes": [{"name": name, "rows": parse_shape(name)}
                       for name in SHAPE_LIBRARY]}


def api_shape(params):
    from .search import (find_shape, parse_shape, plan_shape_workers,
                         render_shape, shape_odds)
    tape = _need_tape()
    limit = min(_int(params, "limit", 0) or len(tape), len(tape))
    shape = parse_shape(_str(params, "shape", "square3"))
    rule = _str(params, "bits", "half")
    max_width = max(4, min(_int(params, "maxWidth", 128), 512))
    span, height = len(shape[0]), len(shape)
    widths = max(0, max_width - span + 1)
    expected, even_odds = shape_odds(shape, limit, widths)
    workers = plan_shape_workers(widths, limit, span,
                                 profile_machine().workers)

    started = time.perf_counter()
    hit, tried = find_shape(tape, shape, limit=limit, max_width=max_width,
                            rule=rule, workers=workers)
    payload = {
        "shape": shape, "cells": span * height, "widths": tried,
        "workers": workers, "seconds": time.perf_counter() - started,
        "expected": expected, "even_odds": even_odds, "searched": limit,
        "one_in": 2 ** (span * height), "found": bool(hit),
    }
    if hit:
        width, offset = hit
        payload.update(width=width, position=offset + 1,
                       row=offset // width + 1, column=offset % width + 1,
                       preview=[[line, inside, start, length] for
                                line, inside, start, length in
                                render_shape(tape, width, offset, shape,
                                             rule=rule, margin=4)])
    return payload


ROUTES = {
    "/api/status": api_status,
    "/api/compute": api_compute,
    "/api/digits": api_digits,
    "/api/search": api_search,
    "/api/birthday": api_birthday,
    "/api/text": api_text,
    "/api/odds": api_odds,
    "/api/random": api_random,
    "/api/hunt": api_hunt,
    "/api/shapes": api_shapes,
    "/api/shape": api_shape,
}


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"piexplorer/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:                       # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, allow_nan=False, default=_safe).encode()
        self._send(status, body, "application/json")

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = ROUTES.get(parsed.path)
        if route is not None:
            try:
                self._json(route(parse_qs(parsed.query)))
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
            except Exception as exc:                  # noqa: BLE001
                traceback.print_exc()
                self._json({"error": f"{type(exc).__name__}: {exc}"},
                           status=500)
            return

        name = "index.html" if parsed.path in ("/", "") else parsed.path.lstrip("/")
        target = (WEB_ROOT / name).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix, "application/octet-stream"))


def _safe(value):
    """JSON has no infinity and no NaN; the odds tables produce both."""
    if isinstance(value, float):
        if math.isinf(value):
            return None
        if math.isnan(value):
            return None
    raise TypeError(f"not serialisable: {type(value).__name__}")


def serve(host: str = "127.0.0.1", port: int = 8765,
          verbose: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose                           # type: ignore[attr-defined]
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        EXPLORER.release()
