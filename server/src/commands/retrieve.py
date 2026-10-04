"""Save the last N seconds of buffered audio to a file.

Usage: retrieve [seconds] [outfile]   (defaults: 10, xapyra-last-<N>s.mp3)
"""
import urllib.error
import urllib.request
from datetime import datetime

from src import ui


def run(ctx, args: list[str]) -> None:
    if not ctx.engine.running:
        ui.warn("Nothing recording -- the ring buffer only fills while live.")
        return
    try:
        seconds = float(args[0]) if len(args) > 0 else 10.0
    except ValueError:
        ui.error(f"Bad seconds value: {args[0]!r}")
        return
    if not 0 < seconds <= 86400:
        ui.error("Seconds must be in (0, 86400].")
        return
    outfile = args[1] if len(args) > 1 else f"xapyra-last-{seconds:g}s-{datetime.now():%Y%m%d-%H%M%S}.mp3"
    url = f"http://127.0.0.1:{ctx.http_port}/audio?seconds={seconds:g}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        ui.error(f"Server said {e.code}: {e.read().decode(errors='replace')[:120]}")
        return
    except OSError as e:
        ui.error(f"Could not reach the server: {e}")
        return
    if not data:
        ui.warn("Ring buffer is empty -- device not streaming?")
        return
    with open(outfile, "wb") as fh:
        fh.write(data)
    ui.success(f"Saved {len(data):,} bytes ({len(data) / 16000:.1f}s) to {outfile}")
