"""Long-lived capture engine: the audio pipeline in a worker thread.

The REPL (src/app.py) is a blocking readline loop, so the asyncio pipeline
runs here on its own thread with its own event loop. The flag CLI
(src/cli.py) keeps its single-loop path and shares `serve_forever` with us --
one serve path, two front ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.audio.link import DeviceLink
from src.audio.recorder import Recorder
from src.audio.ring import AudioRing
from src.audio.server import RetrievalService

LOG = logging.getLogger("xapyra.engine")


@dataclass
class Config:
    device_host: str = "127.0.0.1"
    device_port: int = 8080
    reconnect_max: float = 10.0
    http_host: str = "0.0.0.0"
    http_port: int = 8081
    retention: float = 120.0
    bitrate_kbps: int = 128
    outdir: Path = field(default_factory=lambda: Path("recordings"))
    filename: str | None = None
    record: bool = True


async def serve_forever(link: DeviceLink, svc: RetrievalService, recorder: Recorder, stop: asyncio.Event) -> None:
    """Run the link until `stop` fires, then tear everything down cleanly."""
    device_task = asyncio.create_task(link.run(), name="device-link")
    await stop.wait()
    link.close()
    device_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await device_task
    recorder.stop()
    await svc.close()


def final_timing_report(link: DeviceLink, recorder: Recorder) -> dict | None:
    """Shared shutdown summary for the CLI and REPL stop paths."""
    return link.final_report(recorder)


class Engine:
    """Owns one capture run. Not reusable after stop -- build a new one."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._link: DeviceLink | None = None
        self._report: dict | None = None
        self.config: Config | None = None
        self.http_port: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: Config) -> int:
        """Spin up the pipeline thread. Returns the bound HTTP port."""
        if self.running:
            raise RuntimeError("engine already running -- stop it first")
        self.config = cfg
        ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(ready,), daemon=True, name="xapyra-engine")
        self._thread.start()
        if not ready.wait(timeout=15.0):
            raise RuntimeError("engine thread failed to start")
        assert self.http_port is not None
        return self.http_port

    def stop(self) -> dict | None:
        """Halt capture, join the thread, return the 1:1 report (or None)."""
        if not self.running or self._loop is None or self._stop is None:
            return self._report
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=15.0)
        self._thread = None
        return self._report

    def status(self) -> dict:
        """`/api/status`-shaped snapshot, safe to call from any thread."""
        if self._link is None:
            return {"running": False}
        link = self._link
        timing = link.timing_stats()
        st = {
            "running": True,
            "device": {
                "host": link.host,
                "port": link.port,
                "connected": link.connected,
                "stalled": timing.get("stalled", False),
                "sessions": link.sessions,
                "session_bytes": link.session_bytes,
                "total_bytes": link.total_bytes,
                "connected_since": None,
                "last_error": link.last_error,
            },
            "timing": timing,
            "ring": link.ring.stats(),
            "recording": link.recorder.stats() if link.record else {"active": False},
            "server": {"host": self.config.http_host if self.config else "?", "port": self.http_port},
        }
        return st

    # -- worker thread ------------------------------------------------------ #
    def _thread_main(self, ready: threading.Event) -> None:
        cfg = self.config
        assert cfg is not None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._run(loop, ready)
        except Exception:  # noqa: BLE001 -- report, don't silently die
            LOG.exception("engine thread failed")
        finally:
            self._loop = None
            with contextlib.suppress(Exception):
                loop.close()

    def _run(self, loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
        cfg = self.config
        assert cfg is not None
        ring = AudioRing(cfg.retention, cfg.bitrate_kbps)
        recorder = Recorder(cfg.outdir, cfg.filename)
        link = DeviceLink(
            cfg.device_host, cfg.device_port, ring, recorder,
            cfg.record, cfg.reconnect_max, cfg.bitrate_kbps,
        )
        svc = RetrievalService(cfg.http_host, cfg.http_port, cfg.outdir, link, ring, recorder, time.time())
        stop = asyncio.Event()

        async def _boot() -> None:
            await svc.start()
            self._link = link
            self._stop = stop
            self.http_port = svc.port
            ready.set()

        try:
            loop.run_until_complete(_boot())
        except Exception:
            ready.set()
            raise
        try:
            loop.run_until_complete(serve_forever(link, svc, recorder, stop))
        finally:
            self._report = link.final_report(recorder)
