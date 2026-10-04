"""Minimal stdlib HTTP layer serving live audio, windows and recordings."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, urlsplit

from src.audio._common import (
    CRLF,
    IDLE_STREAM_TIMEOUT,
    IO_CHUNK,
    MAX_HEADER_BYTES,
    REASONS,
    iso,
)
from src.audio.recorder import list_recordings

LOG = logging.getLogger("xapyra.http")

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
        timing = self.link.timing_stats()
        return {
            "device": {
                "host": self.link.host,
                "port": self.link.port,
                "connected": self.link.connected,
                "stalled": timing.get("stalled", False),
                "sessions": self.link.sessions,
                "session_bytes": self.link.session_bytes,
                "total_bytes": self.link.total_bytes,
                "connected_since": iso(self.link.connected_since),
                "last_error": self.link.last_error,
            },
            "timing": timing,
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

