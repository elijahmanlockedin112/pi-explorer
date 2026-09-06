"""Command line front end for the pi explorer."""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from pathlib import Path

from . import __version__
from .engine import (BYTES_PER_DIGIT, DEFAULT_DIGITS, Tape, compute_pi,
                     ensure_tape, estimate_seconds, max_safe_digits,
                     plan_workers, profile_machine, suggested_digits,
                     vault_meta, vault_path, verify_digits, window,
                     write_vault)
from .stats import prob_appears, expected_hits
from .ui import (BLOCK, C, COLOR, DOT, SIGMA, banner, commas, fg, human_count,
                 human_time, kv, meter, note, paint_digits, rule, table,
                 term_width, verdict, warn)

# ---------------------------------------------------------------------------
# shared context
# ---------------------------------------------------------------------------


class Context:
    """Holds the open tape so the interactive shell reuses one mmap."""

    def __init__(self, workers: int | None = None):
        self.workers = workers
        self._tape: Tape | None = None

    def tape(self, want: int | None = None, chatty: bool = True) -> Tape:
        if self._tape is None:
            self._tape = ensure_tape(want, workers=self.workers, chatty=chatty)
        elif want and want > len(self._tape):
            self._tape.close()
            self._tape = ensure_tape(want, workers=self.workers, chatty=chatty)
        return self._tape

    def release(self) -> None:
        """Drop the memory map.

        Windows will not let anything replace a file that is still mapped, so
        every code path that rewrites the vault has to let go of it first --
        otherwise `compute` after a `find` dies with 'Access is denied'.
        """
        if self._tape is not None:
            self._tape.close()
            self._tape = None

    # Kept as the old name; releasing and reloading are the same operation
    # here, since the tape is reopened lazily on next use.
    reload = release


def _window(tape: Tape, args) -> int:
    return window(tape, getattr(args, "digits", None))


# ---------------------------------------------------------------------------
# compute / vault management
# ---------------------------------------------------------------------------

def cmd_compute(ctx: Context, args) -> int:
    profile = profile_machine()
    print(rule("machine"))
    print(kv("hardware", profile.describe()))
    print(kv("digit ceiling", f"{commas(max_safe_digits())} "
                              f"{C.GREY}(RAM-limited){C.RESET}"))

    target = args.digits
    if args.auto or not target:
        target = suggested_digits(patience=args.patience)
        print(kv("auto target", f"{commas(target)} "
                                f"{C.GREY}(~{human_time(estimate_seconds(target))}"
                                f"){C.RESET}"))

    planned = plan_workers(target, ctx.workers)
    print(kv("workers", f"{planned} of {profile.threads} "
                        f"{C.GREY}(sized to the job){C.RESET}"))

    if target > max_safe_digits():
        raise SystemExit(
            f"{C.RED}{commas(target)} digits needs about "
            f"{target*BYTES_PER_DIGIT/(1024**3):.1f} GB. "
            f"Ceiling here is {commas(max_safe_digits())}.{C.RESET}")

    have = vault_meta().get("digits", 0)
    if have >= target and not args.force:
        print(note(f"\nvault already holds {commas(have)} digits. "
                   f"Use --force to recompute."))
        return 0

    estimate = estimate_seconds(target)
    print(rule("compute"))
    if estimate > 20:
        print(note(f"estimated {human_time(estimate)} -- "
                   f"go make a coffee.\n"))
    # Let go of the old digits before building the new ones: it frees the
    # memory, and on Windows it is the difference between replacing the vault
    # file and being told access is denied.
    ctx.release()
    started = time.perf_counter()
    digits = compute_pi(target, workers=ctx.workers, chatty=True)
    elapsed = time.perf_counter() - started

    print(rule("verify"))
    ok = verify_digits(digits)
    print(kv("first 100 digits", f"{C.GREEN}match the known value{C.RESET}" if ok
             else f"{C.RED}MISMATCH{C.RESET}"))
    if not ok:
        raise SystemExit("refusing to store digits that are not pi")
    write_vault(digits, "chudnovsky", elapsed)
    ctx.reload()
    print(kv("stored", f"{commas(len(digits))} digits -> {vault_path()}"))
    print(kv("last digits", paint_digits(digits[-40:])))
    return 0


def cmd_info(ctx: Context, args) -> int:
    profile = profile_machine()
    meta = vault_meta()
    print(banner(__version__))
    print()
    print(rule("machine"))
    print(kv("hardware", profile.describe()))
    print(kv("workers", str(profile.workers)))
    print(kv("digit ceiling", commas(max_safe_digits())))
    print(kv("30s of patience", f"{commas(suggested_digits())} digits"))
    print(rule("vault"))
    if not meta.get("digits"):
        print(note("  empty. Run:  pi compute --auto"))
        return 0
    n = meta["digits"]
    print(kv("digits held", commas(n)))
    print(kv("on disk", f"{vault_path().stat().st_size/1e6:.1f} MB"))
    print(kv("source", str(meta.get("source", "?"))))
    print(kv("computed", str(meta.get("computed_at", "?"))))
    if meta.get("seconds"):
        print(kv("took", f"{human_time(meta['seconds'])} "
                         f"({commas(int(n/meta['seconds']))} digits/sec)"))
    with ensure_tape(chatty=False) as tape:
        print(kv("starts", "3." + paint_digits(tape.get(0, 48)) +
                 f"{C.GREY}...{C.RESET}"))
        print(kv("ends", f"{C.GREY}...{C.RESET}" + paint_digits(
            tape.get(max(0, len(tape) - 40), 40))))
    print(rule("what fits in here"))
    for length, label in ((6, "6-digit string"), (8, "any birthday (MMDDYYYY)"),
                          (10, "a phone number"), (16, "a card number")):
        chance = prob_appears(n, length)
        print(kv(label, f"{chance*100:8.4f}% chance of being in there"))
    return 0


def cmd_bench(ctx: Context, args) -> int:
    profile = profile_machine()
    print(rule("benchmark"))
    print(kv("hardware", profile.describe()))
    sizes = args.sizes or [50_000, 250_000, 1_000_000]
    rows = []
    for n in sizes:
        started = time.perf_counter()
        digits = compute_pi(n, workers=ctx.workers, chatty=False)
        elapsed = time.perf_counter() - started
        rows.append([commas(n), human_time(elapsed),
                     commas(int(n / elapsed)) + "/s",
                     f"{C.GREEN}ok{C.RESET}" if verify_digits(digits)
                     else f"{C.RED}BAD{C.RESET}"])
        print(f"  {commas(n):>12} digits  {human_time(elapsed):>8}  "
              f"{commas(int(n/elapsed)):>10} digits/sec")
    if len(sizes) >= 2:
        big, small = sizes[-1], sizes[0]
        print(note(f"\n  scaling exponent measured over "
                   f"{commas(small)}..{commas(big)} digits"))
    print(note(f"  projected: 10M in {human_time(estimate_seconds(10_000_000))}, "
               f"100M in {human_time(estimate_seconds(100_000_000))}"))
    return 0


def cmd_import(ctx: Context, args) -> int:
    source = Path(args.file)
    if not source.exists():
        raise SystemExit(f"{source} does not exist")
    print(note(f"reading {source} ({source.stat().st_size/1e6:.1f} MB)"))
    raw = source.read_text(errors="ignore")
    digits = re.sub(r"[^0-9]", "", raw)
    if digits.startswith("3"):
        digits = digits[1:]
    if not verify_digits(digits):
        raise SystemExit(f"{C.RED}That file does not start with pi.{C.RESET}")
    ctx.release()          # unmap before replacing the vault file
    write_vault(digits, f"imported:{source.name}", 0.0)
    print(f"  {C.GREEN}imported {commas(len(digits))} digits{C.RESET}")
    return 0


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------

def render_search(tape: Tape, res, show_all: bool = False,
                  label: str | None = None) -> None:
    name = label or res.needle
    print()
    print(f"  {C.BOLD}{paint_digits(res.needle) if not label else name}{C.RESET}"
          f"   {C.GREY}{len(res.needle)} digits{C.RESET}")
    print(f"  {C.FAINT}{'-' * min(70, term_width()-4)}{C.RESET}")

    per_position = 10 ** len(res.needle)
    if res.found:
        print(kv("first appearance",
                 f"{C.GREEN}{C.BOLD}position {commas(res.position)}{C.RESET}"))
        print(kv("occurrences",
                 f"{C.BOLD}{commas(res.count)}{C.RESET} "
                 f"{C.GREY}in {commas(res.searched)} digits{C.RESET}"))
        print(kv("expected", f"{res.expected:,.2f} "
                             f"{C.GREY}(1 in {commas(per_position)} per position)"
                             f"{C.RESET}"))
        if res.luck < 0.5:
            luck = f"{C.CYAN}turned up {1/res.luck:.1f}x sooner than expected{C.RESET}"
        elif res.luck > 2:
            luck = f"{C.ORANGE}had to dig {res.luck:.1f}x deeper than expected{C.RESET}"
        else:
            luck = f"{C.GREY}right about where it should be{C.RESET}"
        print(kv("luck", luck))
        if res.expected >= 1:
            print(kv("count sanity", verdict(res.p_value)))

        before, hit, after = _context(tape, res.first, len(res.needle))
        print(kv("in context", f"{C.GREY}...{C.RESET}{paint_digits(before)}"
                               f"{C.HIT}{hit}{C.RESET}"
                               f"{paint_digits(after)}{C.GREY}...{C.RESET}"))
        if show_all and res.positions:
            shown = ", ".join(commas(p + 1) for p in res.positions)
            more = (f" {C.GREY}(+{commas(res.count - len(res.positions))} more)"
                    f"{C.RESET}") if res.truncated else ""
            print(kv("positions", shown + more))
    else:
        print(kv("first appearance", f"{C.RED}not in the first "
                                     f"{commas(res.searched)} digits{C.RESET}"))
        print(kv("expected", f"{res.expected:,.2f} occurrences "
                             f"{C.GREY}(1 in {commas(per_position)}){C.RESET}"))
        absent = math.exp(-res.expected)
        print(kv("odds of that", f"{absent*100:.1f}% "
                                 f"{C.GREY}-- missing is "
                                 f"{'normal' if absent > 0.05 else 'unusual'} here"
                                 f"{C.RESET}"))
        need = 0.693 * per_position
        if need > res.searched:
            print(kv("even-odds depth", f"{commas(int(need))} digits "
                                        f"{C.GREY}({human_count(need)}) -- "
                                        f"{need/max(res.searched,1):.0f}x more "
                                        f"than you have{C.RESET}"))
            print(note("  Not a surprise, just out of reach. "
                       "Compute deeper and it turns up."))
        else:
            print(kv("even-odds depth", f"{commas(int(need))} digits "
                                        f"{C.GREY}-- you are already "
                                        f"{res.searched/max(need,1):.1f}x past "
                                        f"that{C.RESET}"))
            print(note("  This one is simply running late. Pi does not owe "
                       "you a schedule."))


def _context(tape: Tape, offset: int, length: int, pad: int = 22):
    start = max(0, offset - pad)
    return (tape.get(start, offset - start), tape.get(offset, length),
            tape.get(offset + length, pad))


def cmd_find(ctx: Context, args) -> int:
    from .search import scan
    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    print(rule(f"searching {commas(limit)} digits"))
    for needle in args.needle:
        try:
            res = scan(tape, needle, limit=limit, collect=args.list)
        except ValueError as exc:
            print(warn(str(exc)))
            continue
        render_search(tape, res, show_all=args.all)
    print()
    return 0


def cmd_birthday(ctx: Context, args) -> int:
    from .search import date_variants, parse_date, scan
    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    try:
        year, month, day = parse_date(" ".join(args.date))
    except ValueError as exc:
        raise SystemExit(str(exc))

    print(rule(f"{year}-{month:02d}-{day:02d} in {commas(limit)} digits"))
    rows = []
    for style, digits in date_variants(year, month, day).items():
        res = scan(tape, digits, limit=limit, collect=1)
        if res.found:
            where = f"{C.GREEN}position {commas(res.position)}{C.RESET}"
            hits = commas(res.count)
        else:
            where = f"{C.RED}not found{C.RESET}"
            hits = "0"
        rows.append([style, paint_digits(digits), where, hits,
                     f"{res.odds_of_appearing*100:.2f}%"])
    print()
    print(table(["format", "digits", "first appearance", "hits", "odds"], rows,
                aligns=["<", "<", "<", ">", ">"]))
    print()
    print(note("  Every date ever, and every date to come, is in there "
               "somewhere -- an 8-digit date is even money by 69 million digits "
               "and near certain by 1.5 billion."))
    return 0


def cmd_text(ctx: Context, args) -> int:
    from .search import (decode_letters, encode_text, letter_stream, mode_help,
                         scan)
    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    words = [w for w in args.words]

    print(rule(f"text mode: {args.mode}"))
    print(note(f"  {mode_help(args.mode)}"))

    if args.mode == "pairs":
        started = time.perf_counter()
        print(note(f"  decoding {commas(limit//2)} letters..."), end=" ",
              flush=True)
        book = letter_stream(tape, limit)
        print(note(f"{human_time(time.perf_counter()-started)}"))
        for word in words:
            clean = re.sub(r"[^a-z]", "", word.lower())
            if not clean:
                continue
            at = book.find(clean)
            expected_letters = 26 ** len(clean)
            print()
            print(f"  {C.BOLD}{clean}{C.RESET}   "
                  f"{C.GREY}{len(clean)} letters{C.RESET}")
            print(f"  {C.FAINT}{'-' * min(70, term_width()-4)}{C.RESET}")
            if at >= 0:
                total = book.count(clean)
                print(kv("first appearance",
                         f"{C.GREEN}{C.BOLD}letter {commas(at+1)}{C.RESET} "
                         f"{C.GREY}(digit {commas(at*2+1)}){C.RESET}"))
                print(kv("occurrences", commas(total)))
                print(kv("expected at", f"letter {commas(expected_letters)} "
                                        f"{C.GREY}(digit "
                                        f"{commas(expected_letters*2)}){C.RESET}"))
                left = book[max(0, at - 18):at]
                right = book[at + len(clean):at + len(clean) + 18]
                print(kv("in context", f"{C.GREY}...{left}{C.RESET}"
                                       f"{C.HIT}{clean}{C.RESET}"
                                       f"{C.GREY}{right}...{C.RESET}"))
            else:
                print(kv("first appearance",
                         f"{C.RED}not in the first {commas(limit//2)} letters"
                         f"{C.RESET}"))
                print(kv("expected at", f"letter {commas(expected_letters)} "
                                        f"{C.GREY}= {commas(expected_letters*2)} "
                                        f"digits deep{C.RESET}"))
                print(kv("you would need",
                         f"{human_count(expected_letters*2)} digits "
                         f"{C.GREY}({expected_letters*2/max(limit,1):.0f}x "
                         f"what you have){C.RESET}"))
    else:
        for word in words:
            digits = encode_text(word, args.mode)
            if not digits:
                continue
            res = scan(tape, digits, limit=limit, collect=args.list)
            render_search(tape, res, show_all=args.all,
                          label=f"{C.BOLD}{word}{C.RESET} "
                                f"{C.GREY}-> {digits}{C.RESET}")
    print()
    return 0


def cmd_decode(ctx: Context, args) -> int:
    from .search import decode_letters
    tape = ctx.tape()
    text = decode_letters(tape, args.position - 1, args.count)
    print(rule(f"pi as letters from digit {commas(args.position)}"))
    print()
    for i in range(0, len(text), 72):
        print("  " + text[i:i + 72])
    print()
    return 0


def cmd_dump(ctx: Context, args) -> int:
    tape = ctx.tape()
    start = max(0, args.position - 1)
    text = tape.get(start, args.count)
    per = args.width
    print(rule(f"digits {commas(start+1)} - {commas(start+len(text))}"))
    for i in range(0, len(text), per):
        print(f"  {C.FAINT}{start+i+1:>12,}{C.RESET}  "
              f"{paint_digits(text[i:i+per])}")
    return 0


def cmd_odds(ctx: Context, args) -> int:
    n = args.digits
    if not n:
        meta = vault_meta()
        n = meta.get("digits") or DEFAULT_DIGITS
    print(rule(f"the odds inside {commas(n)} digits"))
    print()
    rows = []
    for length in range(1, 15):
        expected = expected_hits(n, length)
        chance = prob_appears(n, length)
        even = 0.693 * 10 ** length
        if chance > 0.9999:
            odds = f"{C.GREEN}certain{C.RESET}"
        elif chance > 0.5:
            odds = f"{C.CYAN}{chance*100:.2f}%{C.RESET}"
        elif chance > 0.01:
            odds = f"{C.YELLOW}{chance*100:.2f}%{C.RESET}"
        else:
            odds = f"{C.RED}{chance*100:.4g}%{C.RESET}"
        rows.append([str(length), f"1 in {commas(10**length)}",
                     f"{expected:,.2f}", odds, human_count(even)])
    print(table(["digits", "odds per spot", "expected hits", "will it appear?",
                 "even-odds depth"], rows,
                aligns=[">", ">", ">", ">", ">"]))
    print()
    print(rule("things people look for"))
    print()
    examples = [
        ("a 4-digit PIN", 4), ("a birthday MMDDYYYY", 8),
        ("a phone number", 10), ("a social security number", 9),
        ("a 16-digit card number", 16),
    ]
    rows = []
    for label, length in examples:
        chance = prob_appears(n, length)
        rows.append([label, str(length), f"{chance*100:.4g}%",
                     human_count(0.693 * 10 ** length)])
    print(table(["what", "digits", f"in your {commas(n)}", "even-odds depth"],
                rows, aligns=["<", ">", ">", ">"]))
    print()
    print(note("  Every one of these is in there somewhere, if pi is normal. "
               "Nobody has proved that it is."))
    return 0


# ---------------------------------------------------------------------------
# randomness
# ---------------------------------------------------------------------------

def cmd_random(ctx: Context, args) -> int:
    from .lab import SOURCES, battery, pair_matrix, synthesize
    from .art import heatmap

    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    if limit > args.cap and not args.digits:
        limit = args.cap
    data = tape.raw(0, limit)

    print(rule(f"randomness lab -- {commas(limit)} digits"))
    print(note("  Pi is conjectured to be 'normal': every digit and every "
               "block equally likely, forever. Unproven. Let's look."))

    started = time.perf_counter()
    results, counts = battery(data, deep=args.deep)
    print(note(f"  battery finished in "
               f"{human_time(time.perf_counter()-started)}"))

    print()
    print(rule("digit frequencies"))
    print()
    expected = limit / 10
    for digit in range(10):
        count = counts[digit]
        deviation = (count - expected) / math.sqrt(expected * 0.9)
        frac = count / limit
        print(f"  {C.BOLD}{digit}{C.RESET}  {commas(count):>12}  "
              f"{frac*100:7.4f}%  {meter(deviation)}  "
              f"{C.GREY}{deviation:+5.2f}{SIGMA}{C.RESET}")
    print(note(f"\n  centre line = exactly {commas(int(expected))} "
               f"(10.0000%); the gauge runs to four sigma either side."))

    print()
    print(rule("test battery on pi"))
    print()
    rows = []
    for res in results:
        rows.append([res.name, res.detail,
                     verdict(res.p) if res.p is not None
                     else f"{C.GREY}informational{C.RESET}"])
    print(table(["test", "statistic", "verdict"], rows))
    print()
    for res in results:
        if res.note:
            print(f"    {C.GREY}{res.name}: {res.note}{C.RESET}")

    if args.vs:
        print()
        print(rule("control group -- same tests, known random sources"))
        print(note("  If pi were hiding a pattern, its p-values would look "
                   "different from these. They never do."))
        print()
        columns = {"pi": results}
        for kind in args.vs:
            control = synthesize(kind, limit)
            columns[kind], _ = battery(control, deep=args.deep)
        keys = [r.key for r in results]
        names = {r.key: r.name for r in results}
        header = ["test"] + [SOURCES.get(k, k) for k in columns]
        rows = []
        for key in keys:
            row = [names[key]]
            for source, res_list in columns.items():
                match = next((r for r in res_list if r.key == key), None)
                if match is None or match.p is None:
                    row.append(f"{C.GREY}--{C.RESET}")
                else:
                    color = (C.RED if match.p < 0.001 else
                             C.ORANGE if match.p < 0.01 else
                             C.YELLOW if match.p < 0.05 else C.GREEN)
                    row.append(f"{color}{match.p:.4f}{C.RESET}")
            rows.append(row)
        print(table(header, rows, aligns=["<"] + [">"] * len(columns)))

    scored = [r for r in results if r.p is not None]
    failures = [r for r in scored if r.p < 0.01]
    print()
    print(rule("verdict"))
    print(kv("tests run", str(len(scored))))
    print(kv("failed at p<0.01", f"{len(failures)} "
                                 f"{C.GREY}(expect ~{len(scored)*0.01:.1f} "
                                 f"by chance){C.RESET}"))
    if not failures:
        print(f"\n  {C.GREEN}{C.BOLD}Pi passed everything.{C.RESET} "
              f"{C.GREY}Across {commas(limit)} digits there is no test here "
              f"that can tell it apart from a hardware random number "
              f"generator.{C.RESET}")
    else:
        print(f"\n  {C.ORANGE}Flagged: "
              f"{', '.join(r.name for r in failures)}{C.RESET}")
        print(note("  Run it again on a different window before believing it -- "
                   "with this many tests, the occasional low p-value is the "
                   "expected behaviour, not a discovery."))
    print(note("\n  Nobody has ever proved pi is normal. Nobody has ever found "
               "where it breaks either."))

    if args.matrix:
        print()
        print(rule("which digit follows which"))
        print()
        print(heatmap(pair_matrix(data)))
    return 0


# ---------------------------------------------------------------------------
# pattern hunting
# ---------------------------------------------------------------------------

def cmd_hunt(ctx: Context, args) -> int:
    from . import hunt as H
    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    print(rule(f"pattern hunt -- {commas(limit)} digits"))

    print()
    print(f"  {C.BOLD}The Feynman Point{C.RESET}")
    feynman = tape.get(H.FEYNMAN_POINT - 1, 6)
    print(kv("position 762", paint_digits(feynman) +
             (f"  {C.GREEN}six 9s in a row{C.RESET}" if feynman == "999999"
              else f"  {C.RED}?!{C.RESET}")))
    print(note("  Feynman wanted to recite pi to here, then say 'and so on' "
               "to imply it was rational."))

    print()
    print(f"  {C.BOLD}Longest repeats{C.RESET}")
    runs = H.long_runs(tape, limit, minimum=args.min_run)
    if runs:
        for finding in runs[:8]:
            print(kv(finding.label,
                     f"{paint_digits(finding.detail)}  "
                     f"{C.GREY}at {commas(finding.position)}{C.RESET}"))
    else:
        print(note(f"  no runs of {args.min_run}+ in this window"))

    print()
    print(f"  {C.BOLD}Longest palindrome{C.RESET}")
    started = time.perf_counter()
    pal = H.longest_palindrome(tape, min(limit, args.palindrome_cap))
    if pal:
        print(kv(pal.label, f"{paint_digits(pal.detail)} "
                            f"{C.GREY}at {commas(pal.position)} "
                            f"({human_time(time.perf_counter()-started)})"
                            f"{C.RESET}"))

    print()
    print(f"  {C.BOLD}Self-locating strings{C.RESET}")
    print(note("  numbers that begin at their own position"))
    for finding in H.self_locating(tape, min(limit, args.self_cap), cap=8):
        print(kv(f"'{finding.detail}'", f"starts at position "
                                        f"{commas(finding.position)}"))

    print()
    print(f"  {C.BOLD}Staircases{C.RESET}")
    for finding in H.staircases(tape, limit):
        print(kv(finding.label, f"{paint_digits(finding.detail)} "
                                f"{C.GREY}at {commas(finding.position)}{C.RESET}"))

    print()
    print(f"  {C.BOLD}Digit deserts{C.RESET}")
    print(note("  longest stretch that never uses a given digit"))
    for finding in H.digit_deserts(tape, limit)[:3]:
        print(kv(finding.label, f"{finding.detail} "
                                f"{C.GREY}from {commas(finding.position)}"
                                f"{C.RESET}"))

    print()
    print(f"  {C.BOLD}The hold-outs{C.RESET}")
    for k in (3, 4, 5):
        if 10 ** k > limit // 5:
            break
        latest, at, missing_examples, missing = H.most_elusive(tape, k, limit)
        if at < 0:
            print(kv(f"{k}-digit strings",
                     f"{commas(missing)} never show up "
                     f"{C.GREY}(e.g. {', '.join(missing_examples)}){C.RESET}"))
        else:
            print(kv(f"{k}-digit hold-out",
                     f"'{latest}' hides until position {commas(at)}"))
    print()
    return 0


# ---------------------------------------------------------------------------
# pictures
# ---------------------------------------------------------------------------

def cmd_wall(ctx: Context, args) -> int:
    from .art import PALETTES, wall_png, wall_terminal
    tape = ctx.tape(args.digits)
    rule_name = args.bits if args.bits != "none" else None

    if args.out:
        started = time.perf_counter()
        path, w, h = wall_png(tape, args.out, cols=args.cols, rows=args.rows,
                              scale=args.scale, palette=args.palette,
                              offset=args.offset, rule=rule_name)
        print(rule("the wall"))
        print(kv("image", f"{path}"))
        print(kv("size", f"{w} x {h} px"))
        print(kv("digits shown", commas((w // args.scale) * (h // args.scale))))
        print(kv("palette", rule_name or args.palette))
        print(kv("written in", human_time(time.perf_counter() - started)))
        print(note("\n  Every pixel is one digit of pi. It looks like static "
                   "because, as far as anyone can tell, it is."))
        return 0

    print(rule(f"pi from digit {commas(args.offset+1)}"))
    print(wall_terminal(tape, offset=args.offset, cols=args.cols_term,
                        rows=args.rows_term, rule=rule_name))
    print(note("  one block per digit. --out wall.png for the full picture."))
    return 0


def cmd_walk(ctx: Context, args) -> int:
    from .art import braille_walk, walk_svg
    tape = ctx.tape(args.steps)
    steps = min(args.steps, len(tape))
    if args.svg:
        path = walk_svg(tape, args.svg, steps, offset=args.offset)
        print(rule("random walk"))
        print(kv("svg", str(path)))
        print(kv("steps", commas(steps)))
        return 0
    print(rule(f"pi's random walk -- {commas(steps)} steps"))
    print(note("  each digit is a heading: 0-9 spread around the compass"))
    print()
    print(braille_walk(tape, steps, offset=args.offset, height=args.height))
    print()
    print(note("  colour runs purple -> pink with distance travelled. "
               "--svg walk.svg to print it."))
    return 0


def cmd_rain(ctx: Context, args) -> int:
    from .art import rain
    tape = ctx.tape()
    print("\x1b[2J", end="")
    rain(tape, seconds=args.seconds, offset=args.offset)
    return 0


def cmd_shapes(ctx: Context, args) -> int:
    from .search import SHAPE_LIBRARY
    print(rule("built-in shapes"))
    print()
    for name, shape in SHAPE_LIBRARY.items():
        cells = len(shape) * len(shape[0])
        print(f"  {C.BOLD}{name:<10}{C.RESET} {C.GREY}{len(shape[0])}x"
              f"{len(shape)}, 1 in {commas(2**cells)} per spot{C.RESET}")
        for row in shape:
            painted = "".join((fg(255) + BLOCK) if ch == "#"
                              else (C.FAINT + DOT) for ch in row)
            print(f"    {painted}{C.RESET}")
        print()
    print(note("  or draw your own:  pi shape '.##./####/#..#'"))
    return 0


def cmd_shape(ctx: Context, args) -> int:
    from .search import (find_shape, parse_shape, plan_shape_workers,
                         render_shape, shape_odds)
    tape = ctx.tape(args.digits)
    limit = _window(tape, args)
    try:
        shape = parse_shape(args.shape)
    except ValueError as exc:
        raise SystemExit(str(exc))

    height, span = len(shape), len(shape[0])
    cells = height * span
    widths = max(0, args.max_width - max(span, args.min_width or span) + 1)
    expected, even_odds = shape_odds(shape, limit, widths)

    print(rule(f"shape hunt -- {commas(limit)} digits as black and white"))
    print()
    for row in shape:
        print("    " + "".join((fg(255) + BLOCK) if ch == "1"
                               else (C.FAINT + DOT) for ch in row) + C.RESET)
    print()
    print(kv("grid", f"{span} x {height} = {cells} pixels"))
    print(kv("odds per spot", f"1 in {commas(2**cells)}"))
    print(kv("bit rule", f"{args.bits} "
                         f"{C.GREY}({'0-4 dark, 5-9 light' if args.bits=='half' else 'even dark, odd light'}){C.RESET}"))
    planned = plan_shape_workers(widths, limit, span,
                                 profile_machine().workers, args.workers)
    print(kv("row widths tried", f"{widths} "
                                 f"{C.GREY}({max(span, args.min_width or span)}"
                                 f"-{args.max_width}){C.RESET}"))
    print(kv("workers", f"{planned} {C.GREY}(sized to the work){C.RESET}"))
    print(kv("expected finds", f"{expected:.4f}"))
    print(note(f"  reflowing the same digits into different row widths is what "
               f"makes this findable at all -- it multiplies your chances by "
               f"{widths}."))

    started = time.perf_counter()
    print(note(f"\n  scanning on {planned} "
               f"{'core' if planned == 1 else 'cores'}..."), end=" ", flush=True)
    hit, tried = find_shape(tape, shape, limit=limit,
                            min_width=args.min_width, max_width=args.max_width,
                            rule=args.bits, workers=planned)
    print(note(human_time(time.perf_counter() - started)))

    print()
    if not hit:
        print(f"  {C.RED}Not found.{C.RESET}")
        print(kv("you would need", f"{human_count(even_odds)} digits "
                                   f"{C.GREY}for an even-money shot{C.RESET}"))
        print(note("  Try a smaller grid, a wider --max-width, or more digits."))
        return 0

    width, offset = hit
    print(f"  {C.GREEN}{C.BOLD}FOUND{C.RESET} at digit "
          f"{C.BOLD}{commas(offset+1)}{C.RESET} "
          f"{C.GREY}when pi is laid out {width} pixels wide{C.RESET}")
    print(kv("row", commas(offset // width + 1)))
    print(kv("column", commas(offset % width + 1)))
    print()
    for line, inside, start, span_ in render_shape(tape, width, offset, shape,
                                                   rule=args.bits):
        painted = []
        for i, ch in enumerate(line):
            hot = inside and start <= i < start + span_
            if ch == "1":
                painted.append(fg(231 if hot else 244)
                               + (BLOCK if COLOR else ("#" if hot else ":")))
            else:
                painted.append((fg(238) if hot else C.FAINT)
                               + (DOT if COLOR else ("." if hot else " ")))
        print("    " + "".join(painted) + C.RESET)
    print()
    print(note("  bright = your shape, dim = the digits around it."))
    return 0


def cmd_quiz(ctx: Context, args) -> int:
    from .game import quiz
    quiz(ctx.tape(), study=args.study)
    return 0


def cmd_serve(ctx: Context, args) -> int:
    from .server import serve
    url = f"http://{args.host}:{args.port}/"
    print(rule("pi explorer, in a browser"))
    print(kv("machine", profile_machine().describe()))
    meta = vault_meta()
    print(kv("vault", f"{commas(meta.get('digits', 0))} digits"
                      if meta.get("digits") else "empty -- compute from the UI"))
    print(kv("serving", f"{C.BOLD}{url}{C.RESET}"))
    print(note("  localhost only. Ctrl-C to stop."))
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        serve(args.host, args.port, verbose=args.verbose)
    except OSError as exc:
        raise SystemExit(f"{C.RED}could not bind {url}: {exc}{C.RESET}")
    return 0


def cmd_shell(ctx: Context, args) -> int:
    from .shell import repl
    return repl(ctx)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi",
        description="Dig around inside the digits of pi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  pi compute --auto              size the run to this machine and go
  pi find 123456 8675309         where do these live, and how often
  pi birthday 2006-07-09         your birthday, in every date format
  pi text nate math space        words hidden in pi read as letters
  pi random --vs mt os           is pi as random as a real RNG?
  pi shape smiley                find a picture in pi's black-and-white form
  pi wall --out pi.png           paint a million digits as an image
  pi hunt                        Feynman point, palindromes, self-locators
  pi serve                       the web UI, at localhost:8765
  pi                             interactive shell
""")
    parser.add_argument("--version", action="version",
                        version=f"pi-explorer {__version__}")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="cores to use (default: all but one)")
    parser.add_argument("--no-color", action="store_true",
                        help="strip ANSI colour")
    sub = parser.add_subparsers(dest="command")

    def digits_flag(p, help="how many digits to search (default: all of them)"):
        p.add_argument("-d", "--digits", type=int, default=None, help=help)

    p = sub.add_parser("compute", help="compute and store digits of pi")
    p.add_argument("digits", nargs="?", type=int, default=None)
    p.add_argument("--auto", action="store_true",
                   help="pick a target from this machine's RAM and speed")
    p.add_argument("--patience", type=float, default=30.0,
                   help="seconds you are willing to wait for --auto")
    p.add_argument("--force", action="store_true", help="recompute anyway")
    p.set_defaults(func=cmd_compute)

    p = sub.add_parser("info", help="what is in the vault and what fits")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("bench", help="time this machine's digit factory")
    p.add_argument("sizes", nargs="*", type=int)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("import", help="load a digit file from elsewhere")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("find", help="find a digit string, count every hit")
    p.add_argument("needle", nargs="+")
    p.add_argument("-a", "--all", action="store_true",
                   help="list individual positions")
    p.add_argument("--list", type=int, default=24,
                   help="how many positions to keep")
    digits_flag(p)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("birthday", help="find a date in every format")
    p.add_argument("date", nargs="+")
    digits_flag(p)
    p.set_defaults(func=cmd_birthday)

    p = sub.add_parser("text", help="find words hidden in the digits")
    p.add_argument("words", nargs="+")
    p.add_argument("-m", "--mode", choices=("pairs", "a1z26", "ascii"),
                   default="pairs")
    p.add_argument("-a", "--all", action="store_true")
    p.add_argument("--list", type=int, default=12)
    digits_flag(p)
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("decode", help="read pi as letters from a position")
    p.add_argument("position", type=int)
    p.add_argument("count", nargs="?", type=int, default=300)
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("dump", help="print raw digits")
    p.add_argument("position", type=int)
    p.add_argument("count", nargs="?", type=int, default=500)
    p.add_argument("--width", type=int, default=80)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("odds", help="the probability tables")
    digits_flag(p, "digit count to compute odds for")
    p.set_defaults(func=cmd_odds)

    p = sub.add_parser("random", aliases=["test"],
                       help="run the randomness battery on pi")
    digits_flag(p)
    p.add_argument("--cap", type=int, default=1_000_000,
                   help="default window when -d is not given")
    p.add_argument("--deep", action="store_true",
                   help="also test 5- and 6-digit blocks (slower)")
    p.add_argument("--vs", nargs="*", default=["mt", "os"],
                   choices=("mt", "os"),
                   help="control sources to compare against")
    p.add_argument("--matrix", action="store_true",
                   help="show the digit-pair heatmap")
    p.set_defaults(func=cmd_random)

    p = sub.add_parser("hunt", help="Feynman point, palindromes, self-locators")
    digits_flag(p)
    p.add_argument("--min-run", type=int, default=7)
    p.add_argument("--palindrome-cap", type=int, default=1_000_000)
    p.add_argument("--self-cap", type=int, default=1_000_000)
    p.set_defaults(func=cmd_hunt)

    p = sub.add_parser("wall", help="paint the digits")
    p.add_argument("--out", help="write a PNG here instead of the terminal")
    p.add_argument("--cols", type=int, default=1000, help="digits per row (PNG)")
    p.add_argument("--rows", type=int, default=None)
    p.add_argument("--scale", type=int, default=1, help="pixels per digit")
    p.add_argument("--palette", default="spectrum",
                   choices=("spectrum", "pastel", "fire", "ice", "mono", "bw",
                            "parity"))
    p.add_argument("--bits", default="none", choices=("none", "half", "parity"),
                   help="collapse to black and white first")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--cols-term", type=int, default=None)
    p.add_argument("--rows-term", type=int, default=24)
    digits_flag(p)
    p.set_defaults(func=cmd_wall)

    p = sub.add_parser("walk", help="draw pi as a random walk")
    p.add_argument("steps", nargs="?", type=int, default=50_000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--height", type=int, default=30)
    p.add_argument("--svg", help="write an SVG instead")
    p.set_defaults(func=cmd_walk)

    p = sub.add_parser("rain", help="matrix rain, made of pi")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_rain)

    p = sub.add_parser("shape", help="find a picture in pi")
    p.add_argument("shape", help="a library name, or '.##./####/#..#'")
    p.add_argument("--bits", default="half", choices=("half", "parity"))
    p.add_argument("--min-width", type=int, default=0)
    p.add_argument("--max-width", type=int, default=128)
    p.add_argument("--workers", type=int, default=None)
    digits_flag(p)
    p.set_defaults(func=cmd_shape)

    p = sub.add_parser("shapes", help="list the built-in shapes")
    p.set_defaults(func=cmd_shapes)

    p = sub.add_parser("quiz", help="how many digits can you type?")
    p.add_argument("--study", type=int, default=0,
                   help="show this many digits first")
    p.set_defaults(func=cmd_quiz)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="open a browser too")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log every request")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("shell", help="interactive mode")
    p.set_defaults(func=cmd_shell)
    return parser


def run(argv: list[str] | None = None, ctx: Context | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        C.strip()
    if not getattr(args, "command", None):
        from .shell import repl
        return repl(ctx or Context(workers=args.workers))
    context = ctx or Context(workers=args.workers)
    if args.workers and ctx is None:
        context.workers = args.workers
    return args.func(context, args) or 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print(f"\n{C.GREY}interrupted{C.RESET}")
        return 130
    except BrokenPipeError:
        return 0
