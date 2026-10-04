#!/usr/bin/env python3
"""Xapyra interactive shell: `python server/main.py [--simple]`.

Fullscreen chatbox by default (betterVpack-style). `--simple` selects the
linear fallback for terminals that ignore ANSI cursor positioning.
"""
import sys

from src.app import start

if __name__ == "__main__":
    start(simple="--simple" in sys.argv[1:])
