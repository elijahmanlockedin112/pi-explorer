"""The interactive shell -- the search menu you actually sit in front of."""

from __future__ import annotations

import re
import shlex
import sys

from . import __version__
from .engine import vault_meta
from .ui import C, banner, commas, note, paint_digits, rule

PROMPT = f"{C.PURPLE}pi{C.RESET}{C.GREY}>{C.RESET} "

HELP = f"""
{C.BOLD}search{C.RESET}
  {C.CYAN}1234{C.RESET}                    bare digits search straight away
  {C.CYAN}find 123456 8675309{C.RESET}     first position + how many times
  {C.CYAN}find 314 --all{C.RESET}          list every position
  {C.CYAN}birthday 2006-07-09{C.RESET}     a date in every format
  {C.CYAN}text nate math space{C.RESET}    words, reading pi two digits per letter
  {C.CYAN}odds{C.RESET}                    what is findable in what you have

{C.BOLD}is it random?{C.RESET}
  {C.CYAN}random{C.RESET}                  the full battery, vs a real RNG
  {C.CYAN}random --deep --matrix{C.RESET}  block tests up to 6 digits + heatmap
  {C.CYAN}hunt{C.RESET}                    Feynman point, palindromes, hold-outs

{C.BOLD}look at it{C.RESET}
  {C.CYAN}wall{C.RESET}                    digits as colour, right here
  {C.CYAN}wall --out pi.png --scale 2{C.RESET}
  {C.CYAN}shape smiley{C.RESET}            find a picture in the digits
  {C.CYAN}shapes{C.RESET}                  the shape library
  {C.CYAN}walk 200000{C.RESET}             pi as a random walk
  {C.CYAN}rain{C.RESET}                    digit rain

{C.BOLD}housekeeping{C.RESET}
  {C.CYAN}compute --auto{C.RESET}          more digits, sized to this machine
  {C.CYAN}compute 5000000{C.RESET}         exactly this many
  {C.CYAN}info{C.RESET}   {C.CYAN}bench{C.RESET}   {C.CYAN}dump 1000 200{C.RESET}   {C.CYAN}quiz{C.RESET}
  {C.CYAN}help{C.RESET}   {C.CYAN}quit{C.RESET}
"""


def _greeting() -> str:
    meta = vault_meta()
    held = meta.get("digits", 0)
    if held:
        return (f"{C.GREY}vault: {C.RESET}{C.BOLD}{commas(held)}{C.RESET}"
                f"{C.GREY} digits ready. Type {C.RESET}help{C.GREY} for the "
                f"menu, or just type a number.{C.RESET}")
    return (f"{C.GREY}vault is empty. Start with {C.RESET}compute --auto"
            f"{C.GREY} -- then search it.{C.RESET}")


def repl(ctx) -> int:
    from .cli import run

    try:
        import readline  # noqa: F401  (gives arrow-key history where available)
    except Exception:
        pass

    print(banner(__version__))
    print()
    print(_greeting())
    print()

    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in {"quit", "exit", "q", ":q"}:
            break
        if low in {"help", "?", "h"}:
            print(HELP)
            continue
        if low in {"clear", "cls"}:
            print("\x1b[2J\x1b[H", end="")
            continue

        # Typing a bare number means "find it" -- the thing you came here to do.
        if re.fullmatch(r"[\d ,]+", line):
            line = "find " + re.sub(r"[ ,]+", " ", line).strip()

        try:
            argv = shlex.split(line)
        except ValueError as exc:
            print(f"{C.RED}{exc}{C.RESET}")
            continue

        try:
            run(argv, ctx=ctx)
        except SystemExit as exc:
            if exc.code not in (0, None):
                message = str(exc)
                if message and not message.isdigit():
                    print(f"{C.RED}{message}{C.RESET}")
        except KeyboardInterrupt:
            print(f"\n{C.GREY}stopped{C.RESET}")
        except Exception as exc:  # keep the shell alive whatever happens
            print(f"{C.RED}{type(exc).__name__}: {exc}{C.RESET}")

    print(note("bye. pi is still going."))
    return 0
