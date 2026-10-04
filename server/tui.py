#!/usr/bin/env python3
"""Live terminal dashboard for the Xapyra retrieval server.

Style adapted from betterVpack (github.com/daniswastaken/betterVpack): plain
ANSI colours, no curses, no third-party packages -- so it works on the Windows
console as-is. The box redraws in place a few times per second; Ctrl+C quits.

Layout contract for `render()`: fixed-height ASCII box, driven purely by the
`/api/status` dict shape, so it is unit-testable without a running server.
"""

from __future__ import annotations

import asyncio
import sys

# -- palette (betterVpack levels, plus DIM for chrome) ----------------------- #
RED = "\033[31m"
GREEN = "\033[92m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

WIDTH = 64
HEIGHT = 9  # render() always emits this many lines


def message(text: str, level: int = 1) -> None:
    """betterVpack-compatible status line. 1 info, 2 success, 3 warning, 4 error."""
    if level == 2:
        print(f"{BOLD}{GREEN}Success:{RESET} {text}")
    elif level == 3:
        print(f"{BOLD}{YELLOW}Warning:{RESET} {text}")
    elif level == 4:
        print(f"{BOLD}{RED}Error:{RESET} {text}")
    else:
        print(text)


def _row(text: str) -> str:
    text = text[: WIDTH - 4]
    return f"| {text.ljust(WIDTH - 4)} |"


def _bar(frac: float, width: int = 20) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = round(frac * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def render(st: dict) -> str:
    """Fixed-height dashboard box from an `/api/status`-shaped dict."""
    dev = st.get("device", {})
    timing = st.get("timing", {})
    ring = st.get("ring", {})
    rec = st.get("recording", {})

    live = bool(dev.get("connected"))
    dot = f"{GREEN}LIVE{RESET}" if live else f"{RED}DOWN{RESET}"
    head = f"XAPYRA {dev.get('host', '?')}:{dev.get('port', '?')}  [{dot}]"

    total = dev.get("total_bytes", 0) or 0
    obs = timing.get("observed_bytes_per_sec")
    rate = f"{obs:,.0f} B/s" if obs else "-- B/s"
    line2 = f"session #{dev.get('sessions', 0)}  {total:,} bytes  {rate}"

    wall = timing.get("wall_seconds", 0.0) or 0.0
    audio = timing.get("audio_seconds", 0.0) or 0.0
    drift = timing.get("drift_seconds", 0.0) or 0.0
    drift_col = GREEN if abs(drift) < 1.0 else (YELLOW if abs(drift) < 5.0 else RED)
    line3 = f"1:1 wall {wall:.1f}s  audio {audio:.1f}s  drift {drift_col}{drift:+.1f}s{RESET}"

    retained = ring.get("retention_seconds", 0) or 0
    buffered = ring.get("buffered_seconds", 0) or 0
    frac = (buffered / retained) if retained else 0.0
    line4 = f"ring {_bar(frac)} {buffered:.1f}/{retained:g}s"

    if rec.get("active"):
        rname = rec.get("path") or "?"
        # total_written is the in-memory truth; file_bytes lags behind stdio
        # buffering (64 KB chunks) and bytes_written resets every session.
        rbytes = rec.get("total_written")
        if not rbytes:
            rbytes = rec.get("file_bytes")
        if rbytes is None:
            rbytes = rec.get("bytes_written", 0) or 0
        line5 = f"REC {rname}  {rbytes:,} bytes"
    else:
        line5 = "REC idle (--no-record or link down)"

    err = dev.get("last_error")
    line6 = f"last error: {err}" if err else "last error: none"

    foot = f"{DIM}Ctrl+C quit | http://localhost:{st.get('server', {}).get('port', 8081)}/ | green LED blinks while LIVE{RESET}"

    border = "+" + "-" * (WIDTH - 2) + "+"
    lines = [
        border,
        _row(f"{BOLD}{head}{RESET}"),
        _row(line2),
        _row(line3),
        _row(line4),
        _row(line5),
        _row(line6),
        border,
        foot,
    ]
    return "\n".join(lines)


async def dashboard(link, http_port: int, stop: asyncio.Event, interval: float = 0.5) -> None:
    """Redraw the box in place until `stop` fires. All state is polled, so a
    dead link degrades to DOWN instead of killing the display."""
    first = True
    while not stop.is_set():
        st = {
            "device": {
                "host": link.host,
                "port": link.port,
                "connected": link.connected,
                "sessions": link.sessions,
                "total_bytes": link.total_bytes,
                "last_error": link.last_error,
            },
            "timing": link.timing_stats(),
            "ring": link.ring.stats(),
            "recording": link.recorder.stats()
            if link.record
            else {"active": False, "path": None, "bytes_written": 0, "file_bytes": None},
            "server": {"port": http_port},
        }
        box = render(st)
        if first and sys.stdout.isatty():
            sys.stdout.write("\n" * (HEIGHT - 1))
            first = False
        if sys.stdout.isatty():
            sys.stdout.write(f"\033[{HEIGHT - 1}A\033[J")
        sys.stdout.write(box + "\n")
        sys.stdout.flush()
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
