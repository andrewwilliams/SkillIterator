"""
Minimal logging module for Skill Iterator.

Simple print-to-stderr logging with a verbose toggle.
No stdlib `logging` — too heavy for a CLI tool.
"""

from __future__ import annotations

import sys

_verbose = False


def set_verbose(flag: bool) -> None:
    global _verbose
    _verbose = flag


def is_verbose() -> bool:
    return _verbose


def debug(msg: str) -> None:
    """Print to stderr only when verbose mode is on."""
    if _verbose:
        print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """Always print warning to stderr."""
    print(f"Warning: {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    """Always print error to stderr."""
    print(f"Error: {msg}", file=sys.stderr, flush=True)
