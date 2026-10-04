#!/usr/bin/env python3
"""Fake Xapyra: stands in for the ESP32 so you can test the retrieval server
without hardware.

Speaks exactly what src/main.cpp speaks -- a raw MPEG-1 Layer III byte stream
(128 kbps, 32 kHz, mono) pushed over TCP with no framing -- and announces the
IP in the same "IP_EVENT_STA_GOT_IP, IP: x.x.x.x" form the firmware prints.

    python fake_device.py --port 8080
    python retrieval_server.py --device-host 127.0.0.1

Frame geometry mirrors Shine's output at 32 kHz/128 kbps: 576 bytes per frame,
1152 samples, 36 ms. Frames are zero-filled, which decoders render as silence.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import sys
import time
from datetime import datetime

LOG = logging.getLogger("xapyra.fake-device")

SAMPLE_RATE = 32000
BITRATE = 128000
FRAME_BYTES = 144 * BITRATE // SAMPLE_RATE  # 576
FRAME_SECONDS = 1152 / SAMPLE_RATE  # 0.036
FRAME = bytes([0xFF, 0xFB, 0x94, 0xC0]) + b"\x00" * (FRAME_BYTES - 4)


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def feed(writer: asyncio.StreamWriter, speed: float, duration: float) -> None:
    # Deadline-paced: each tick sleeps only the remainder to its slot, so
    # drain() overhead and coarse timer granularity don't accumulate into a
    # slow source. Long-run rate is exact, which the 1:1 check depends on.
    per_tick = max(1, round(0.05 / FRAME_SECONDS))
    loop = asyncio.get_running_loop()
    end = None if duration <= 0 else loop.time() + duration / speed
    start = loop.time()
    sent = 0
    while True:
        now = loop.time()
        if end is not None and now >= end:
            return
        writer.write(FRAME * per_tick)
        await writer.drain()
        sent += per_tick
        if sent % (per_tick * 20) == 0:  # ~1s of audio
            LOG.info("streamed %.1fs of audio", sent * FRAME_SECONDS)
        slot = start + (sent * FRAME_SECONDS) / speed
        delay = slot - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, speed: float, duration: float) -> None:
    peer = writer.get_extra_info("peername")
    start = time.monotonic()
    LOG.info("client connected: %s", peer)
    try:
        await feed(writer, speed, duration)
    except (ConnectionResetError, BrokenPipeError):
        LOG.info("client hung up")
    else:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
    LOG.info("session over after %.1fs", time.monotonic() - start)


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    ip = args.host if args.host not in ("0.0.0.0", "") else local_ip()
    print(f"IP_EVENT_STA_GOT_IP, IP: {ip}")
    print(f"TCP server listening on port {args.port}")

    server = await asyncio.start_server(lambda r, w: handle(r, w, args.speed, args.duration), args.host, args.port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):
            pass
    async with server:
        await stop.wait()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    p.add_argument("--port", type=int, default=8080, help="bind port")
    p.add_argument("--speed", type=float, default=1.0, help="stream faster than realtime; handy for filling the ring in tests")
    p.add_argument("--duration", type=float, default=0.0, help="close the connection after N seconds of audio (0 = forever)")
    p.add_argument("--at", default=datetime.now().strftime("%H:%M:%S"), help="cosmetic: startup timestamp")
    sys.exit(asyncio.run(main(p.parse_args())))
