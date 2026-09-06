#!/usr/bin/env python3
"""Launcher:  python pi.py find 123456

Kept as a plain script so the whole thing runs straight out of a clone with
no install step and no dependencies.
"""

import sys

from pi_explorer.cli import main

if __name__ == "__main__":
    # The guard matters: worker processes re-import this file on Windows.
    sys.exit(main())
