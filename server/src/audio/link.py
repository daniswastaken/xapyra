"""TCP client of the ESP32 stream + first-byte 1:1 clock."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import AsyncIterator

from src.audio._common import (
    IDLE_SESSION_TIMEOUT,
    IO_CHUNK,
    SUBSCRIBER_QUEUE,
    iso,
)

LOG = logging.getLogger("xapyra.link")

# Read loop polls in short slices so a quiet-but-open socket is noticed fast:
# STALL_AFTER warns (link up, no bytes), IDLE_SESSION_TIMEOUT drops it.
READ_POLL = 1.0
STALL_AFTER = 5.0

# --------------------------------------------------------------------------- #
class DeviceLink:
    """Holds the TCP connection to the device and fans bytes out to subscribers."""

    def __init__(
        self,
        host: str,
        port: int,
        ring: AudioRing,
        recorder: Recorder,
        record: bool,
        reconnect_max: float,
        bitrate_kbps: int = 128,
    ) -> None:
        self.host = host
        self.port = port
        self.ring = ring
        self.recorder = recorder
        self.record = record
        self.reconnect_max = reconnect_max
        self.bytes_per_sec = bitrate_kbps * 1000 // 8
        self.connected = False
        self.sessions = 0
        self.session_bytes = 0
        self.total_bytes = 0
        self.last_error: str | None = None
        self.connected_since: float | None = None
        # 1:1 clock -- starts on the very first audio byte, not on connect.
        # Wall time from here must match audio time (bytes / bytes_per_sec).
        # `connected_seconds` accumulates only time spent inside a live
        # session, so dropouts show up as `gap_seconds` instead of silently
        # inflating drift on long runs.
        self.first_byte_mono: float | None = None
        self.first_byte_wall: float | None = None
        self.session_start_mono: float | None = None
        self.session_first_byte_mono: float | None = None
        self.last_byte_mono: float | None = None
        self._stall_warned = False
        self.connected_seconds = 0.0
        self._subscribers: set[asyncio.Queue] = set()
        self._closing = False

    def timing_stats(self) -> dict:
        """Wall time vs audio time since the first byte. Drift ~0 means 1:1.

        Two drifts are reported because they mean different things:
        - drift_vs_connected: audio minus time spent inside live sessions.
          Near 0 means every connected second became audio (no frame loss).
        - drift_seconds: audio minus total wall (includes dropout gaps).
          Negative here with drift_vs_connected near 0 means the link was
          down for a while -- time with no audio to record, not lost audio.
        """
        now_mono = time.monotonic()
        bps = self.bytes_per_sec or 1
        connected = self.connected_seconds
        if self.connected and self.session_first_byte_mono is not None:
            connected += now_mono - self.session_first_byte_mono
        audio = self.total_bytes / bps
        if self.last_byte_mono is not None:
            seconds_since_byte = now_mono - self.last_byte_mono
        elif self.connected:
            seconds_since_byte = now_mono - (self.session_start_mono or now_mono)
        else:
            seconds_since_byte = None
        stalled = bool(
            self.connected
            and seconds_since_byte is not None
            and seconds_since_byte >= STALL_AFTER
        )
        if self.first_byte_mono is not None:
            wall = now_mono - self.first_byte_mono
            gap = wall - connected
            out = {
                "first_byte_at": iso(self.first_byte_wall),
                "wall_seconds": round(wall, 3),
                "connected_seconds": round(connected, 3),
                "gap_seconds": round(gap, 3),
                "audio_seconds": round(audio, 3),
                "drift_seconds": round(audio - wall, 3),
                "drift_ratio": round((audio - wall) / wall, 5) if wall > 0 else None,
                "drift_vs_connected": round(audio - connected, 3),
                "seconds_since_byte": round(seconds_since_byte, 3) if seconds_since_byte is not None else None,
                "stalled": stalled,
                "observed_bytes_per_sec": round(self.total_bytes / wall, 1) if wall > 0 else None,
                "observed_connected_bps": round(self.total_bytes / connected, 1) if connected > 0 else None,
                "expected_bytes_per_sec": bps,
            }
        else:
            out = {
                "first_byte_at": None,
                "wall_seconds": 0.0,
                "connected_seconds": 0.0,
                "gap_seconds": 0.0,
                "audio_seconds": 0.0,
                "drift_seconds": 0.0,
                "drift_ratio": None,
                "drift_vs_connected": 0.0,
                "seconds_since_byte": round(seconds_since_byte, 3) if seconds_since_byte is not None else None,
                "stalled": stalled,
                "observed_bytes_per_sec": None,
                "observed_connected_bps": None,
                "expected_bytes_per_sec": bps,
            }
        if self.session_first_byte_mono is not None:
            swall = now_mono - self.session_first_byte_mono
            saudio = self.session_bytes / bps
            out["session"] = {
                "wall_seconds": round(swall, 3),
                "audio_seconds": round(saudio, 3),
                "drift_seconds": round(saudio - swall, 3),
            }
        return out

    def final_report(self, recorder) -> dict | None:
        """Shutdown summary, or None when no audio ever arrived."""
        t = self.timing_stats()
        if t.get("first_byte_at") is None:
            return None
        name = recorder.path.name if getattr(recorder, "path", None) else "(not recorded)"
        return {"file": name, **t}

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    def _fanout(self, data: bytes) -> None:
        for q in list(self._subscribers):
            if q.full():  # slow consumer: drop oldest, keep the stream live
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(data)

    def close(self) -> None:
        self._closing = True

    async def run(self) -> None:
        backoff = 1.0
        warned_down = False
        while not self._closing:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=5.0)
            except (OSError, asyncio.TimeoutError) as exc:
                # A device that is off or unreachable is normal operation, not a
                # fault: report it once per outage and stay quiet until it clears.
                # TimeoutError has an empty str(), so name it explicitly --
                # "timed out" and "refused" point at different problems.
                if isinstance(exc, asyncio.TimeoutError) and not str(exc):
                    reason = f"no answer within 5s on {self.host}:{self.port}"
                else:
                    reason = str(exc) or type(exc).__name__
                if not warned_down:
                    LOG.warning("cannot reach device %s:%d -- %s; retrying", self.host, self.port, reason)
                    warned_down = True
                self.last_error = reason
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max)
                continue
            if warned_down:
                LOG.info("device is reachable again")
                warned_down = False
            backoff = 1.0
            try:
                await self._session(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad session must not stop the loop
                LOG.exception("device session ended with an error")
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.connected = True
        self.sessions += 1
        sess = self.sessions  # entry-local: the live counter lies if sessions ever overlap
        self.session_bytes = 0
        self.connected_since = time.time()
        self.session_start_mono = time.monotonic()
        self.session_first_byte_mono = None
        self._stall_warned = False
        self.last_error = None
        if self.record:
            self.recorder.start()
        LOG.info("device connected: %s (session #%d)", peer, sess)
        try:
            while not self._closing:
                try:
                    data = await asyncio.wait_for(reader.read(IO_CHUNK // 8), timeout=READ_POLL)
                except asyncio.TimeoutError:
                    data = None  # no chunk this poll; stall/drop decided below
                except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                    # Peer vanished mid-read (killed, WiFi drop, router reset). The
                    # socket never reaches EOF, so this raises instead of returning
                    # b"" -- it must be caught here or it escapes run() and kills
                    # the reconnect loop for good.
                    LOG.info("device link lost: %s", exc)
                    break
                now_mono = time.monotonic()
                if data:
                    if self.session_first_byte_mono is None:
                        self.session_first_byte_mono = now_mono
                    if self.first_byte_mono is None:
                        self.first_byte_mono = now_mono
                        self.first_byte_wall = time.time()
                        LOG.info("first audio byte received -- 1:1 timer started")
                    self.last_byte_mono = now_mono
                    self._stall_warned = False
                    self.ring.append(data)
                    if self.record:
                        self.recorder.write(data)
                    self._fanout(data)
                    self.session_bytes += len(data)
                    self.total_bytes += len(data)
                    continue
                # No chunk this poll: data may be an empty EOF or just late.
                if data == b"":
                    LOG.info("device closed the stream")
                    break
                quiet_for = now_mono - (self.last_byte_mono or self.session_start_mono or now_mono)
                if quiet_for >= IDLE_SESSION_TIMEOUT:
                    LOG.warning("device idle for %.0fs, dropping link", quiet_for)
                    break
                if quiet_for >= STALL_AFTER and not self._stall_warned:
                    self._stall_warned = True
                    LOG.warning(
                        "no audio for %.0fs although the link is up -- "
                        "mic/encoder stall or WiFi stall on the device",
                        quiet_for,
                    )
        finally:
            self.connected = False
            self.connected_since = None
            if self.record:
                self.recorder.stop()
            if self.session_first_byte_mono is not None:
                swall = time.monotonic() - self.session_first_byte_mono
                self.connected_seconds += swall
                saudio = self.session_bytes / (self.bytes_per_sec or 1)
                LOG.info(
                    "session #%d 1:1 wall=%.2fs audio=%.2fs drift=%+.2fs (%+.2f%%)",
                    sess,
                    swall,
                    saudio,
                    saudio - swall,
                    (100.0 * (saudio - swall) / swall) if swall > 0 else 0.0,
                )
            else:
                LOG.info("device disconnected after %d bytes (no audio received)", self.session_bytes)

