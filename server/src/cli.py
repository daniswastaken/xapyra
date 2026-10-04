"""Flag-driven front end: `python server/retrieval_server.py [flags]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

from src import wizard
from src.audio._common import iso  # noqa: F401 (re-exported for compat)
from src.audio.link import DeviceLink
from src.audio.recorder import Recorder
from src.audio.ring import AudioRing
from src.audio.server import RetrievalService
from src.engine import final_timing_report, serve_forever

LOG = logging.getLogger("xapyra.cli")

def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="retrieval_server.py",
        description="Retrieve/record/serve the MP3 stream from an Xapyra ESP32.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    dev = p.add_argument_group("device (ESP32 TCP server)")
    dev.add_argument(
        "--device-host",
        default=env("XAPYRA_DEVICE_HOST", "127.0.0.1"),
        help="ESP32 IP, hostname, or 'auto' to scan the local network for port 8080",
    )
    dev.add_argument("--device-port", type=int, default=int(env("XAPYRA_DEVICE_PORT", "8080")), help="ESP32 TCP port")
    dev.add_argument("--reconnect-max", type=float, default=10.0, help="max seconds between reconnect attempts")
    dev.add_argument(
        "--scan-subnet",
        action="append",
        default=None,
        metavar="CIDR",
        help="limit 'auto' discovery to this subnet (repeatable, /24 or smaller). Default: every local /24",
    )
    dev.add_argument(
        "--scan-timeout",
        type=float,
        default=float(env("XAPYRA_SCAN_TIMEOUT", "0.4")),
        help="per-host connect timeout in seconds for 'auto' discovery",
    )
    h = p.add_argument_group("http server")
    h.add_argument("--http-host", default=env("XAPYRA_HTTP_HOST", "0.0.0.0"), help="bind address")
    h.add_argument("--http-port", type=int, default=int(env("XAPYRA_HTTP_PORT", "8081")), help="bind port (0 = pick a free one)")
    a = p.add_argument_group("audio")
    a.add_argument("--retention", type=float, default=float(env("XAPYRA_RETENTION", "120")), help="seconds of audio kept in memory")
    a.add_argument("--bitrate-kbps", type=int, default=int(env("XAPYRA_BITRATE_KBPS", "128")), help="stream bitrate, for the buffer-seconds estimate")
    a.add_argument("--outdir", type=Path, default=Path(env("XAPYRA_OUTDIR", "recordings")), help="where session recordings are written")
    a.add_argument(
        "--file",
        default=os.environ.get("XAPYRA_FILE") or None,
        metavar="NAME",
        help="write all audio to this one .mp3 inside --outdir instead of a new timestamped file per session",
    )
    a.add_argument("--no-record", action="store_true", help="serve and retain audio but write nothing to disk")
    a.add_argument(
        "--run-for",
        type=float,
        default=float(env("XAPYRA_RUN_FOR", "0")),
        metavar="SECONDS",
        help="stop automatically N seconds after the first audio byte (1:1 test). 0 = run until Ctrl+C",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for request logs, -vv for debug")
    p.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    p.add_argument(
        "--tui",
        action="store_true",
        help="live terminal dashboard (betterVpack-style) instead of log lines",
    )
    p.add_argument(
        "--wizard",
        action="store_true",
        help="interactive setup: scan, pick, record (default with no args on a TTY)",
    )
    p.add_argument(
        "--no-wizard",
        action="store_true",
        help="never prompt; fail fast on missing config instead",
    )
    return p


def configure_logging(args: argparse.Namespace) -> None:
    if args.quiet:
        level = logging.WARNING
    elif args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def safe_output_name(name: str) -> str:
    """--file is a bare name inside --outdir; nothing may climb out of it."""
    name = name.strip()
    if not name:
        raise ValueError("--file must not be empty")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("--file must be a plain filename, not a path")
    if not name.endswith(".mp3"):
        name += ".mp3"
    return name


async def amain(args: argparse.Namespace) -> int:
    from src.net.discover import discover_one
    from src.ui import dashboard
    from src.ui import message as say

    if args.device_host.lower() == "auto":
        found = await discover_one(
            args.device_port, subnets=args.scan_subnet, timeout=args.scan_timeout
        )
        if found is None:
            say("no device answering on port "
                f"{args.device_port} -- is the ESP32 on the same network?", 4)
            return 2
        say(f"ESP32 found at {found}", 2)
        args.device_host = found

    ring = AudioRing(args.retention, args.bitrate_kbps)
    filename = args.file
    recorder = Recorder(args.outdir, filename)
    link = DeviceLink(
        args.device_host, args.device_port, ring, recorder,
        not args.no_record, args.reconnect_max, args.bitrate_kbps,
    )
    svc = RetrievalService(args.http_host, args.http_port, args.outdir, link, ring, recorder, time.time())

    try:
        port = await svc.start()
    except OSError as exc:
        LOG.error("cannot bind http://%s:%d -- %s", args.http_host, args.http_port, exc)
        return 2
    LOG.info("http on http://%s:%d/  (device %s:%d, retention %gs)", svc.host, port, link.host, link.port, args.retention)
    if args.no_record:
        LOG.info("recording disabled (--no-record)")
    elif filename:
        LOG.info("recording into %s", (args.outdir / filename))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    tui_task = None
    if args.tui:
        if not args.quiet and args.verbose == 0:
            # Dashboard owns the screen; INFO log lines would tear it up.
            logging.getLogger().setLevel(logging.WARNING)
            say("live dashboard -- Ctrl+C quits", 1)
        tui_task = asyncio.create_task(dashboard(link, port, stop), name="tui")

    run_for_task = None
    if args.run_for and args.run_for > 0:
        LOG.info("1:1 test: will stop %.1fs after the first audio byte", args.run_for)
        run_for_task = asyncio.create_task(_run_for_watcher(link, args.run_for, stop), name="run-for")

    await serve_forever(link, svc, recorder, stop)
    LOG.info("shutting down")
    for task in (tui_task, run_for_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _log_final_timing(link, recorder)
    return 0


async def _run_for_watcher(link: DeviceLink, run_for: float, stop: asyncio.Event) -> None:
    """Fire `stop` N seconds after the first audio byte arrives."""
    while link.first_byte_mono is None and not stop.is_set():
        await asyncio.sleep(0.1)
    if stop.is_set():
        return
    LOG.info("first byte seen, capturing %.1fs...", run_for)
    await asyncio.sleep(run_for)
    LOG.info("--run-for expired, stopping")
    stop.set()


def _log_final_timing(link: DeviceLink, recorder: Recorder) -> None:
    t = final_timing_report(link, recorder)
    if t is None:
        LOG.warning("1:1 report: no audio received, nothing to compare")
        return
    LOG.info(
        "1:1 report file=%s wall=%.2fs connected=%.2fs gap=%.2fs audio=%.2fs "
        "drift=%+.2fs (%+.2f%%) in-session drift=%+.2fs observed=%s B/s expected=%d B/s",
        t["file"],
        t["wall_seconds"],
        t["connected_seconds"],
        t["gap_seconds"],
        t["audio_seconds"],
        t["drift_seconds"],
        (100.0 * (t["drift_ratio"] or 0.0)),
        t["drift_vs_connected"],
        t["observed_bytes_per_sec"],
        t["expected_bytes_per_sec"],
    )
    if t["gap_seconds"] > 1.0 and abs(t["drift_vs_connected"]) < max(5.0, 0.02 * t["connected_seconds"]):
        LOG.info(
            "drift is all dropout gap, not lost audio: %.0fs with no link, "
            "in-session audio 1:1",
            t["gap_seconds"],
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    user_args = argv if argv is not None else sys.argv[1:]
    if args.wizard or (not args.no_wizard and wizard.in_wizard_mode(user_args)):
        from src.ui import message as say
        from src.ui import PickerAbort

        try:
            cfg = asyncio.run(
                wizard.run_wizard(
                    port=args.device_port,
                    subnets=args.scan_subnet,
                    timeout=args.scan_timeout,
                    outdir=str(args.outdir),
                )
            )
        except (PickerAbort, KeyboardInterrupt):
            say("setup cancelled.", 1)
            return 130
        args.device_host = cfg["device_host"]
        args.outdir = Path(cfg["outdir"])
        if cfg["filename"] is not None:
            args.file = cfg["filename"]
        args.tui = True  # wizard always ends in the dashboard
    if args.retention <= 0:
        LOG.error("--retention must be > 0")
        return 2
    if args.bitrate_kbps <= 0:
        LOG.error("--bitrate-kbps must be > 0")
        return 2
    if args.run_for < 0:
        LOG.error("--run-for must be >= 0")
        return 2
    if args.scan_timeout <= 0:
        LOG.error("--scan-timeout must be > 0")
        return 2
    if args.file is not None:  # not `if args.file` -- "" must also be validated
        try:
            args.file = safe_output_name(args.file)
        except ValueError as exc:
            LOG.error("%s", exc)
            return 2
        if args.no_record:
            LOG.warning("--file has no effect with --no-record")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
