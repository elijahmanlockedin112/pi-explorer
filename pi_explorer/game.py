"""How many digits of pi can you actually type?"""

from __future__ import annotations

import sys
import time

from .engine import Tape
from .ui import C, commas, human_time, note, paint_digits

__all__ = ["getch", "quiz"]

# Current record: 70,030 digits, Rajveer Meena, 2015, blindfolded, ten hours.
WORLD_RECORD = 70_030
RANKS = [
    (0, "you have met pi"),
    (3, "you know it is about three"),
    (7, "textbook value"),
    (15, "you have owned a calculator"),
    (30, "genuinely respectable"),
    (50, "showing off"),
    (100, "you practised this"),
    (500, "competitive memoriser"),
    (1000, "please seek help"),
    (10000, "you are on a list somewhere"),
]


def getch():
    """Read one keypress without waiting for Enter. None if unavailable.

    The tty check is load-bearing on Windows: msvcrt reads the console device
    directly, so with piped stdin it would sit there waiting for a key that
    is never coming.
    """
    if not sys.stdin.isatty():
        return None
    if sys.platform == "win32":
        try:
            import msvcrt
            ch = msvcrt.getwch()
            return ch
        except Exception:
            return None
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return None


def _rank(n: int) -> str:
    label = RANKS[0][1]
    for threshold, name in RANKS:
        if n >= threshold:
            label = name
    return label


def quiz(tape: Tape, study: int = 0) -> None:
    """Type pi. Keep typing. See where you break."""
    truth = tape.get(0, 4000)

    print()
    print(f"{C.BOLD}Type the digits of pi.{C.RESET} "
          f"{C.GREY}(the '3.' is free -- start at 1){C.RESET}")
    if study:
        print(f"\n  {paint_digits(truth[:study])}\n")
        print(note("  study up, then press Enter"))
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            return
        print("\x1b[2J\x1b[H", end="")
    print(note("  Esc or Ctrl-C to stop.\n"))

    sys.stdout.write(f"  {C.GREY}3.{C.RESET}")
    sys.stdout.flush()

    correct = 0
    started = None
    interactive = True
    try:
        while True:
            ch = getch()
            if ch is None:
                interactive = False
                break
            if ch in ("\x03", "\x1b", "\r", "\n"):
                break
            if not ch.isdigit():
                continue
            if started is None:
                started = time.perf_counter()
            if ch == truth[correct]:
                correct += 1
                sys.stdout.write(C.GREEN + ch + C.RESET)
                sys.stdout.flush()
                if correct >= len(truth):
                    break
            else:
                sys.stdout.write(C.RED + ch + C.RESET)
                sys.stdout.flush()
                print(f"\n\n  {C.RED}Wrong.{C.RESET} Digit {commas(correct+1)} "
                      f"is {C.GREEN}{truth[correct]}{C.RESET}, not "
                      f"{C.RED}{ch}{C.RESET}.")
                break
    except KeyboardInterrupt:
        pass

    if not interactive:
        # No raw keyboard available (piped stdin, odd terminal): fall back to
        # typing a whole line at once.
        print(note("  (no raw keyboard here -- paste or type a line, then Enter)"))
        try:
            typed = input("  3.")
        except (EOFError, KeyboardInterrupt):
            return
        started = time.perf_counter()
        typed = "".join(c for c in typed if c.isdigit())
        correct = 0
        for a, b in zip(typed, truth):
            if a != b:
                break
            correct += 1

    elapsed = (time.perf_counter() - started) if started else 0.0
    print()
    print(f"  {C.BOLD}{commas(correct)} digits{C.RESET} -- {_rank(correct)}")
    if elapsed > 0.2:
        print(note(f"  {human_time(elapsed)}, "
                   f"{correct/elapsed:.1f} digits/sec"))
    print(note(f"  the world record is {commas(WORLD_RECORD)}. "
               f"You are {WORLD_RECORD/max(correct,1):,.0f}x away."))
    print(f"\n  next up: {paint_digits(truth[correct:correct+40])}{C.GREY}...{C.RESET}\n")
