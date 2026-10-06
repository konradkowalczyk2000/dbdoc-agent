"""Console progress messages with timestamps.

Levels: step (a new action), info, ok, warn, error, detail (only with --verbose).
Indented messages belong to the table currently being analyzed.
"""
from __future__ import annotations

import datetime as dt
import sys

VERBOSE = False
QUIET = False

# Windows consoles/redirects with a legacy code page must not crash on symbols like "→".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


def _emit(symbol: str, message: str, indent: int, to_stderr: bool = False) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    stream = sys.stderr if to_stderr else sys.stdout  # resolved per call, so redirection works
    print(f"[{stamp}] {'    ' * indent}{symbol} {message}", file=stream, flush=True)


def step(message: str, indent: int = 0) -> None:
    """Start of an action, e.g. 'Signing in…', 'Analyzing table dbo.Orders'."""
    if not QUIET or indent == 0:
        _emit("→", message, indent)


def info(message: str, indent: int = 0) -> None:
    if not QUIET:
        _emit("·", message, indent)


def ok(message: str, indent: int = 0) -> None:
    if not QUIET or indent == 0:
        _emit("✓", message, indent)


def warn(message: str, indent: int = 0) -> None:
    _emit("!", message, indent)


def error(message: str, indent: int = 0) -> None:
    _emit("✗", message, indent, to_stderr=True)


def detail(message: str, indent: int = 0) -> None:
    if VERBOSE:
        _emit(" ", message, indent)


def banner(title: str) -> None:
    if not QUIET:
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


def plural(n: int, word: str) -> str:
    """plural(1, 'row') -> '1 row', plural(3, 'row') -> '3 rows'"""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"
