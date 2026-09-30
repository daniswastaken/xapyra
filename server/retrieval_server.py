#!/usr/bin/env python3
"""Xapyra retrieval server.

Pulls the raw MP3 byte stream that the ESP32 publishes (it runs the TCP
server on port 8080 -- see src/main.cpp), keeps a rolling in-memory ring of
the last N seconds, writes everything to disk, and serves the audio back over
HTTP so a client, a voice agent, or a human can *retrieve* what the mic heard.

No third-party packages: stdlib + asyncio only.

    python retrieval_server.py --device-host 192.168.1.42

Endpoints (default http://0.0.0.0:8081):

    GET /                  HTML dashboard (live player, recordings, retrieval)
    GET /stream            live MP3 stream, chunked, follows the device
    GET /audio?seconds=10  MP3 of the last N seconds from the ring buffer
    GET /recordings/<name> saved recording (supports Range requests)
    GET /api/status        JSON: link state, ring fill, recordings
    GET /healthz           200 while the device link is up, 503 while it is down
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("xapyra.retrieval")

MAX_HEADER_BYTES = 16 * 1024
IO_CHUNK = 64 * 1024
CRLF = b"\r\n"
SUBSCRIBER_QUEUE = 512
IDLE_SESSION_TIMEOUT = 20.0
IDLE_STREAM_TIMEOUT = 10.0

REASONS = {
    200: "OK",
    206: "Partial Content",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    416: "Range Not Satisfiable",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def iso(ts: float | None) -> str | None:
    """Local-time ISO stamp, or None."""
    return None if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# audio retention
# --------------------------------------------------------------------------- #
class AudioRing:
    """Rolling window of the last `retention` seconds of MP3 bytes.

    Chunks are kept whole and stamped on arrival, so a window cut lands on a
    chunk boundary -- retrieval is accurate to within one read (a few ms).
    """

    def __init__(self, retention: float, bitrate_kbps: int) -> None:
        self.retention = retention
        self.bytes_per_sec = bitrate_kbps * 1000 // 8
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._size = 0
        self.total_bytes = 0

    def append(self, data: bytes) -> None:
        now = time.monotonic()
        self._chunks.append((now, data))
        self._size += len(data)
        self.total_bytes += len(data)
        cutoff = now - self.retention
        while self._chunks and self._chunks[0][0] < cutoff:
            self._size -= len(self._chunks.popleft()[1])

    def window(self, seconds: float) -> bytes:
        now = time.monotonic()
        cutoff = now - min(seconds, self.retention)
        return b"".join(data for stamp, data in self._chunks if stamp >= cutoff)

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0

    def stats(self) -> dict:
        return {
            "retention_seconds": round(self.retention, 3),
            "buffered_bytes": self._size,
            "buffered_seconds": round(self._size / self.bytes_per_sec, 3) if self.bytes_per_sec else None,
            "total_bytes": self.total_bytes,
        }


# --------------------------------------------------------------------------- #
# disk recording
# --------------------------------------------------------------------------- #
class Recorder:
    """Writes the stream to disk.

    Without `filename` each device session gets its own timestamped file. With
    `filename` (--file) every session appends to one fixed file: opened
    truncating when the server starts, reopened in append mode on each
    reconnect so the file is one continuous capture rather than one file per
    dropout.
    """

    def __init__(self, outdir: Path, filename: str | None = None) -> None:
        self._outdir = outdir
        self._filename = filename
        self._fh = None
        self._opened_once = False
        self.path: Path | None = None
        self.started_at: float | None = None
        self.bytes_written = 0

    def start(self) -> None:
        if self._fh is not None:
            return
        if self._filename is not None:
            self.path = self._outdir / self._filename
            # First open of this server run starts clean; reconnects append.
            mode = "ab" if self._opened_once else "wb"
        else:
            self.path = self._outdir / f"xapyra-{datetime.now():%Y%m%d-%H%M%S}.mp3"
            mode = "wb"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self._fh = self.path.open(mode, buffering=IO_CHUNK)
        self._opened_once = True
        self.bytes_written = 0
        LOG.info("recording -> %s", self.path)

    def write(self, data: bytes) -> None:
        if self._fh is None:
            return
        self._fh.write(data)
        self.bytes_written += len(data)

    def stop(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        LOG.info("recording saved: %s (%d bytes)", self.path, self.bytes_written)
        self._fh = None

    def stats(self) -> dict:
        return {
            "active": self._fh is not None,
            "path": self.path.name if self.path else None,
            "started_at": iso(self.started_at),
            "bytes_written": self.bytes_written,
        }


def list_recordings(outdir: Path) -> list[dict]:
    if not outdir.is_dir():
        return []
    out = []
    for p in sorted(outdir.glob("*.mp3"), key=lambda q: q.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "bytes": st.st_size, "modified": iso(st.st_mtime)})
    return out


# --------------------------------------------------------------------------- #
# device link: TCP client of the ESP32's server
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
        self.first_byte_mono: float | None = None
        self.first_byte_wall: float | None = None
        self.session_start_mono: float | None = None
        self.session_first_byte_mono: float | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._closing = False

    def timing_stats(self) -> dict:
        """Wall time vs audio time since the first byte. Drift ~0 means 1:1."""
        now_mono = time.monotonic()
        bps = self.bytes_per_sec or 1
        out: dict = {}
        if self.first_byte_mono is not None:
            wall = now_mono - self.first_byte_mono
            audio = self.total_bytes / bps
            out = {
                "first_byte_at": iso(self.first_byte_wall),
                "wall_seconds": round(wall, 3),
                "audio_seconds": round(audio, 3),
                "drift_seconds": round(audio - wall, 3),
                "drift_ratio": round((audio - wall) / wall, 5) if wall > 0 else None,
                "observed_bytes_per_sec": round(self.total_bytes / wall, 1) if wall > 0 else None,
                "expected_bytes_per_sec": bps,
            }
        else:
            out = {
                "first_byte_at": None,
                "wall_seconds": 0.0,
                "audio_seconds": 0.0,
                "drift_seconds": 0.0,
                "drift_ratio": None,
                "observed_bytes_per_sec": None,
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
        self.session_bytes = 0
        self.connected_since = time.time()
        self.session_start_mono = time.monotonic()
        self.session_first_byte_mono = None
        self.last_error = None
        if self.record:
            self.recorder.start()
        LOG.info("device connected: %s (session #%d)", peer, self.sessions)
        try:
            while not self._closing:
                try:
                    data = await asyncio.wait_for(reader.read(IO_CHUNK // 8), timeout=IDLE_SESSION_TIMEOUT)
                except asyncio.TimeoutError:
                    LOG.warning("device idle for %.0fs, dropping link", IDLE_SESSION_TIMEOUT)
                    break
                except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                    # Peer vanished mid-read (killed, WiFi drop, router reset). The
                    # socket never reaches EOF, so this raises instead of returning
                    # b"" -- it must be caught here or it escapes run() and kills
                    # the reconnect loop for good.
                    LOG.info("device link lost: %s", exc)
                    break
                if not data:
                    LOG.info("device closed the stream")
                    break
                now_mono = time.monotonic()
                if self.session_first_byte_mono is None:
                    self.session_first_byte_mono = now_mono
                if self.first_byte_mono is None:
                    self.first_byte_mono = now_mono
                    self.first_byte_wall = time.time()
                    LOG.info("first audio byte received -- 1:1 timer started")
                self.ring.append(data)
                if self.record:
                    self.recorder.write(data)
                self._fanout(data)
                self.session_bytes += len(data)
                self.total_bytes += len(data)
        finally:
            self.connected = False
            self.connected_since = None
            if self.record:
                self.recorder.stop()
            if self.session_first_byte_mono is not None:
                swall = time.monotonic() - self.session_first_byte_mono
                saudio = self.session_bytes / (self.bytes_per_sec or 1)
                LOG.info(
                    "session #%d 1:1 wall=%.2fs audio=%.2fs drift=%+.2fs (%+.2f%%)",
                    self.sessions,
                    swall,
                    saudio,
                    saudio - swall,
                    (100.0 * (saudio - swall) / swall) if swall > 0 else 0.0,
                )
            else:
                LOG.info("device disconnected after %d bytes (no audio received)", self.session_bytes)


# --------------------------------------------------------------------------- #
# minimal HTTP layer (no framework)
# --------------------------------------------------------------------------- #
@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]

    def first(self, key: str, default: str | None = None) -> str | None:
        vals = self.query.get(key)
        return vals[0] if vals else default


@dataclass
class Reply:
    status: int = 200
    content_type: str = "text/plain; charset=utf-8"
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class HttpServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sock = self._server.sockets[0]
        return sock.getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    # -- connection handling (Connection: close, one request per socket) ---- #
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            try:
                reply = await self.route(request)
            except HttpError as exc:
                reply = Reply(exc.status, body=exc.message.encode())
            except Exception:  # noqa: BLE001 - never kill the server on one bad request
                LOG.exception("unhandled error serving %s", request.path)
                reply = Reply(500, body=b"internal server error")
            await self._send(reply, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            LOG.exception("connection error")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        try:
            line = await reader.readuntil(CRLF)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionResetError):
            return None
        parts = line.decode("latin-1", "replace").split()
        if len(parts) < 2:
            return None
        headers: dict[str, str] = {}
        used = len(line)
        while used < MAX_HEADER_BYTES:
            try:
                raw = await reader.readuntil(CRLF)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return None
            used += len(raw)
            if raw in (b"\r\n", b"\n"):
                break
            name, _, value = raw.decode("latin-1", "replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        else:
            return None
        split = urlsplit(parts[1])
        return Request(parts[0], split.path or "/", parse_qs(split.query), headers)

    async def _send(self, reply: Reply, writer: asyncio.StreamWriter) -> None:
        if reply.stream is not None:
            await self._send_stream(reply, writer)
            return
        body = reply.body or b""
        head = [
            f"HTTP/1.1 {reply.status} {REASONS.get(reply.status, 'OK')}",
            f"Content-Type: {reply.content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        head += [f"{k}: {v}" for k, v in reply.headers.items()]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

    async def _send_stream(self, reply: Reply, writer: asyncio.StreamWriter) -> None:
        head = [
            f"HTTP/1.1 {reply.status} {REASONS.get(reply.status, 'OK')}",
            f"Content-Type: {reply.content_type}",
            "Transfer-Encoding: chunked",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        head += [f"{k}: {v}" for k, v in reply.headers.items()]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        try:
            async for chunk in reply.stream:  # type: ignore[union-attr]
                if not chunk:
                    continue
                writer.write(f"{len(chunk):X}".encode() + CRLF + chunk + CRLF)
                await writer.drain()
            writer.write(b"0" + CRLF + CRLF)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            with contextlib.suppress(OSError):
                writer.transport.abort()  # type: ignore[union-attr]

    # -- routes ------------------------------------------------------------- #
    async def route(self, request: Request) -> Reply:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
class RetrievalService(HttpServer):
    def __init__(self, http_host: str, http_port: int, outdir: Path, link: DeviceLink, ring: AudioRing, recorder: Recorder, started: float) -> None:
        super().__init__(http_host, http_port)
        self.outdir = outdir
        self.link = link
        self.ring = ring
        self.recorder = recorder
        self.started = started

    async def route(self, request: Request) -> Reply:
        if request.method not in ("GET", "HEAD"):
            raise HttpError(405, "only GET is supported")
        LOG.debug("%s %s", request.method, request.path)
        path = request.path.rstrip("/") or "/"

        if path == "/":
            return Reply(200, "text/html; charset=utf-8", body=self._dashboard().encode())
        if path == "/stream":
            return self._live_stream(request)
        if path == "/audio":
            return self._window(request)
        if path == "/api/status":
            return Reply(200, "application/json; charset=utf-8", body=self._reply_json(self._status()))
        if path == "/healthz":
            status = 200 if self.link.connected else 503
            return Reply(status, body=(b"up" if status == 200 else b"device link down"))
        if path == "/recordings":
            return Reply(200, "application/json; charset=utf-8", body=self._reply_json(list_recordings(self.outdir)))
        if path.startswith("/recordings/"):
            return self._recording(path[len("/recordings/"):], request)
        raise HttpError(404, f"no route for {request.path}")

    # -- handlers ----------------------------------------------------------- #
    def _live_stream(self, request: Request) -> Reply:
        # /stream runs until the device goes quiet, which never happens while it
        # is streaming -- so an unbounded request cannot terminate. With
        # ?seconds=N the relay stops on its own after N seconds, which gives
        # clients (and tests) a well-defined end instead of a hanging read.
        limit = request.first("seconds")
        max_seconds: float | None = None
        if limit is not None:
            try:
                max_seconds = float(limit)
            except ValueError:
                raise HttpError(400, f"bad seconds={limit!r}") from None
            if not 0 < max_seconds <= 86400:
                raise HttpError(400, "seconds must be in (0, 86400]")

        async def gen() -> AsyncIterator[bytes]:
            loop = asyncio.get_running_loop()
            deadline = None if max_seconds is None else loop.time() + max_seconds
            async with self.link.subscribe() as q:
                while True:
                    if deadline is not None:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            return
                        timeout = min(IDLE_STREAM_TIMEOUT, remaining)
                    else:
                        timeout = IDLE_STREAM_TIMEOUT
                    try:
                        data = await asyncio.wait_for(q.get(), timeout=timeout)
                    except asyncio.TimeoutError:
                        # Either the device went quiet or we hit the duration
                        # cap: end cleanly so the client can retry or stop.
                        return
                    yield data

        return Reply(200, "audio/mpeg", stream=gen())

    def _window(self, request: Request) -> Reply:
        raw = request.first("seconds", "10")
        try:
            seconds = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HttpError(400, f"bad seconds={raw!r}") from None
        if not 0 < seconds <= self.ring.retention:
            raise HttpError(400, f"seconds must be in (0, {self.ring.retention:g}]")
        data = self.ring.window(seconds)
        if not data:
            raise HttpError(503, "ring buffer is empty (device not streaming?)")
        return Reply(
            200,
            "audio/mpeg",
            body=data,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="xapyra-last-{seconds:g}s.mp3"',
            },
        )

    def _recording(self, name: str, request: Request) -> Reply:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise HttpError(400, "bad recording name")
        path = self.outdir / name
        if not path.is_file():
            raise HttpError(404, f"no recording named {name}")
        size = path.stat().st_size
        ctype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "application/octet-stream"
        start, end, status = 0, size - 1, 200
        spec = request.headers.get("range", "")
        if spec.startswith("bytes="):
            first, _, last = spec[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                else:
                    raise ValueError
            except ValueError:
                raise HttpError(400, f"bad Range header: {spec!r}") from None
            if size == 0 or start >= size or start > end:
                raise HttpError(416, f"range not satisfiable (size {size})")
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": f'inline; filename="{name}"',
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        async def gen() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    data = fh.read(min(IO_CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return Reply(status, ctype, stream=gen(), headers=headers)

    # -- views -------------------------------------------------------------- #
    @staticmethod
    def _reply_json(payload: object) -> bytes:
        return json.dumps(payload, indent=2).encode()

    def _status(self) -> dict:
        return {
            "device": {
                "host": self.link.host,
                "port": self.link.port,
                "connected": self.link.connected,
                "sessions": self.link.sessions,
                "session_bytes": self.link.session_bytes,
                "total_bytes": self.link.total_bytes,
                "connected_since": iso(self.link.connected_since),
                "last_error": self.link.last_error,
            },
            "timing": self.link.timing_stats(),
            "ring": self.ring.stats(),
            "recording": self.recorder.stats(),
            "recordings": list_recordings(self.outdir),
            "server": {
                "host": self.host,
                "port": self.port,
                "uptime_seconds": round(time.time() - self.started, 1),
            },
        }

    def _dashboard(self) -> str:
        st = self._status()
        dot = "ok" if st["device"]["connected"] else "down"
        rows = "\n".join(
            f'<tr><td><a href="/recordings/{html.escape(r["name"])}">{html.escape(r["name"])}</a></td>'
            f'<td align="right">{r["bytes"]}</td><td>{html.escape(str(r["modified"]))}</td></tr>'
            for r in st["recordings"]
        ) or '<tr><td colspan="3">none yet</td></tr>'
        return f"""<!doctype html>
<meta charset="utf-8">
<title>Xapyra retrieval</title>
<style>
 body{{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:2rem auto;max-width:52rem;padding:0 1rem}}
 h1{{font-size:1.1rem}} a{{color:#0a7}}
 #dot{{font-weight:bold;color:{{'#0a0' if dot == 'ok' else '#a00'}}}}
 audio{{width:100%}} code{{background:#eee;padding:0 .3em}}
 table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #ddd;padding:.3em .5em}}
 form{{margin:.8em 0}}
</style>
<h1>Xapyra retrieval &mdash; <span id="dot">{dot}</span></h1>
<audio controls preload="none" src="/stream"></audio>
<form onsubmit="location.href='/audio?seconds='+this.n.value;return false">
  retrieve last <input name="n" type="number" value="10" min="1" step="1"
   max="{st['ring']['retention_seconds']:g}" style="width:6rem"> seconds
  <input type="submit" value="download MP3">
</form>
<p>ring: {st['ring']['buffered_seconds']}s buffered /
   {st['ring']['retention_seconds']:g}s retained &middot;
   {st['device']['total_bytes']} bytes in {st['device']['sessions']} session(s) &middot;
   1:1 wall {st['timing']['wall_seconds']}s / audio {st['timing']['audio_seconds']}s
   (drift {st['timing']['drift_seconds']}s)</p>
<p>live JSON: <a href="/api/status">/api/status</a></p>
<h2 style="font-size:1rem">recordings</h2>
<table><tr><th>file</th><th align="right">bytes</th><th>modified</th></tr>
{rows}
</table>
<script>
setInterval(async()=>{{
  const d = await (await fetch('/api/status')).json();
  document.getElementById('dot').textContent = d.device.connected ? 'ok' : 'down';
}}, 2000);
</script>
"""


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="retrieval_server.py",
        description="Retrieve/record/serve the MP3 stream from an Xapyra ESP32.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    dev = p.add_argument_group("device (ESP32 TCP server)")
    dev.add_argument("--device-host", default=env("XAPYRA_DEVICE_HOST", "127.0.0.1"), help="ESP32 IP or hostname")
    dev.add_argument("--device-port", type=int, default=int(env("XAPYRA_DEVICE_PORT", "8080")), help="ESP32 TCP port")
    dev.add_argument("--reconnect-max", type=float, default=10.0, help="max seconds between reconnect attempts")
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

    device_task = asyncio.create_task(link.run(), name="device-link")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    run_for_task = None
    if args.run_for and args.run_for > 0:
        LOG.info("1:1 test: will stop %.1fs after the first audio byte", args.run_for)
        run_for_task = asyncio.create_task(_run_for_watcher(link, args.run_for, stop), name="run-for")

    await stop.wait()
    LOG.info("shutting down")
    link.close()
    if run_for_task is not None:
        run_for_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_for_task
    device_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await device_task
    recorder.stop()
    _log_final_timing(link, recorder)
    await svc.close()
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
    t = link.timing_stats()
    if t.get("first_byte_at") is None:
        LOG.warning("1:1 report: no audio received, nothing to compare")
        return
    path = recorder.path.name if recorder.path else "(not recorded)"
    LOG.info(
        "1:1 report file=%s wall=%.2fs audio=%.2fs drift=%+.2fs (%+.2f%%) observed=%s B/s expected=%d B/s",
        path,
        t["wall_seconds"],
        t["audio_seconds"],
        t["drift_seconds"],
        (100.0 * (t["drift_ratio"] or 0.0)),
        t["observed_bytes_per_sec"],
        t["expected_bytes_per_sec"],
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    if args.retention <= 0:
        LOG.error("--retention must be > 0")
        return 2
    if args.bitrate_kbps <= 0:
        LOG.error("--bitrate-kbps must be > 0")
        return 2
    if args.run_for < 0:
        LOG.error("--run-for must be >= 0")
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
