#!/usr/bin/env python3
"""End-to-end checks for the Xapyra retrieval server.

Runs the real server against a fake device, then verifies the HTTP contract,
ring-buffer accuracy, MP3 frame integrity, and reconnect behaviour. No
third-party packages; no hardware needed.

    python test_retrieval.py
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import retrieval_server as rs  # noqa: E402

DEVICE_PORT = 18190
HTTP_PORT = 18191
BASE = f"http://127.0.0.1:{HTTP_PORT}"
FRAME_BYTES = 576

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed.append(f"{name}{(' -- ' + detail) if detail else ''}")
        print(f"  FAIL  {name}{(' -- ' + detail) if detail else ''}")
    return ok


READ_TIMEOUT = 25.0


def _get(path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Blocking HTTP GET. Callers must go through `get`, which runs this in a
    worker thread -- the server under test shares our event loop, so blocking it
    here would deadlock the very thing being tested."""
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post(path: str) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(BASE + path, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


async def get(path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    return await asyncio.to_thread(_get, path, headers)


async def wait_for_get(path: str, want: int, timeout: float, label: str) -> bool:
    """Poll an endpoint for a status code. Async on purpose: the probe runs via
    `get`, which hops to a worker thread, so the server's event loop keeps
    turning while we wait."""
    async def probe() -> bool:
        return (await get(path))[0] == want

    return await wait_for(probe, timeout, label)


async def wait_for(predicate, timeout: float, label: str) -> bool:
    """Poll a sync or async predicate without blocking the shared event loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.2)
    print(f"        (timeout waiting for {label})")
    return False


def frames_valid(data: bytes) -> bool:
    return bool(data) and len(data) % FRAME_BYTES == 0 and all(
        data[i] == 0xFF and data[i + 1] == 0xFB for i in range(0, len(data) - 1, FRAME_BYTES)
    )


def test_safe_output_name() -> list[tuple[str, bool, str]]:
    """Cases for the --file filename guard (unit, no server needed)."""
    from retrieval_server import safe_output_name

    out: list[tuple[str, bool, str]] = []
    for value in ("test123.mp3", "test123", "a b.mp3", "x.MP3"):
        try:
            got = safe_output_name(value)
            out.append((f"--file {value!r} accepted", got.endswith(".mp3"), got))
        except ValueError as exc:
            out.append((f"--file {value!r} accepted", False, f"raised {exc}"))
    for value in ("../evil.mp3", "sub/evil.mp3", "sub\\evil.mp3", "..", ".", ""):
        try:
            safe_output_name(value)
            out.append((f"--file {value!r} rejected", False, "accepted a path"))
        except ValueError:
            out.append((f"--file {value!r} rejected", True, ""))
    return out


async def scenario(tmp: Path, dev: subprocess.Popen) -> None:
    ring = rs.AudioRing(20, 128)
    rec = rs.Recorder(tmp / "rec")
    link = rs.DeviceLink("127.0.0.1", DEVICE_PORT, ring, rec, True, 1.0, 128)
    svc = rs.RetrievalService("127.0.0.1", HTTP_PORT, tmp / "rec", link, ring, rec, time.time())
    http = await svc.start()
    link_task = asyncio.create_task(link.run(), name="device-link")

    try:
        print("\n[1] device link comes up")
        check("link connects to the device", await wait_for(lambda: link.connected, 10, "device connect"))
        check("bytes are arriving", await wait_for(lambda: link.session_bytes > 0, 5, "first bytes"))

        print("\n[2] status endpoint")
        status, headers, body = await get("/api/status")
        check("/api/status returns 200", status == 200, str(status))
        check("/api/status is JSON", headers.get("Content-Type", "").startswith("application/json"))
        d = json.loads(body)
        check("status reports the device connected", d["device"]["connected"] is True)
        check("status reports a live byte count", d["device"]["total_bytes"] > 0)
        check("status exposes retention seconds", d["ring"]["retention_seconds"] == 20.0)

        print("\n[3] healthz")
        code, _, _ = await get("/healthz")
        check("/healthz is 200 while up", code == 200, str(code))

        print("\n[4] ring window accuracy")
        await asyncio.sleep(1.0)
        # Measure the observed byte rate directly off the ring over a short
        # measured interval, rather than mixing time.time()/monotonic() clocks.
        b0, t0 = ring.total_bytes, time.monotonic()
        await asyncio.sleep(2.0)
        rate = (ring.total_bytes - b0) / (time.monotonic() - t0)
        _, _, w2 = await get("/audio?seconds=2")
        got = len(w2) / rate if rate else 0.0
        check("/audio?seconds=2 returns audio", len(w2) > 0)
        check("2s window is ~2s of audio", 1.6 <= got <= 2.4, f"{got:.2f}s")
        check("window is valid MP3", frames_valid(w2), f"{len(w2)} bytes")

        print("\n[4b] 1:1 clock")
        t = link.timing_stats()
        check("first-byte timer started", t.get("first_byte_at") is not None, str(t.get("first_byte_at")))
        check("wall and audio clocks advance", t["wall_seconds"] > 0 and t["audio_seconds"] > 0, str(t))
        # Fake device runs at --speed 4, so observed rate must be ~4x nominal.
        # This proves the clock measures correctly; 1:1 itself is tested live.
        obs = t.get("observed_bytes_per_sec") or 0
        check("observed rate tracks source speed (~4x)", 3 * 16000 < obs < 5 * 16000, str(obs))
        for label, param, want in (
            ("seconds=0 rejected with 400", "seconds=0", 400),
            ("seconds beyond retention rejected", "seconds=9999", 400),
            ("non-numeric seconds rejected", "seconds=abc", 400),
        ):
            code, _, _ = await get(f"/audio?{param}")
            check(label, code == want, str(code))
        code, _, _ = await get("/audio?seconds=1e400")
        check("overflowing seconds rejected", code in (400, 503), str(code))

        print("\n[6] method and routing")
        code, _, _ = await asyncio.to_thread(_post, "/api/status")
        check("POST rejected with 405", code == 405, str(code))
        code, _, _ = await get("/nope")
        check("unknown route 404s", code == 404, str(code))
        code, _, body = await get("/")
        check("dashboard serves HTML", code == 200 and b"<title>" in body, str(code))

        print("\n[7] recordings and Range requests")
        check(
            "recording file written to disk",
            await wait_for(lambda: list((tmp / "rec").glob("*.mp3")), 5, "recording file"),
        )
        files = sorted((tmp / "rec").glob("*.mp3"))
        name = files[0].name
        code, hdrs, body = await get(f"/recordings/{name}")
        check("recording downloads whole", code == 200, str(code))
        check("recording advertises byte ranges", hdrs.get("Accept-Ranges") == "bytes")
        check("recording is valid MP3", frames_valid(body))
        # The session is still being written to, so its length changes between
        # requests. Read the authoritative total from Content-Range of an
        # open-ended range, which the server computes at request time.
        _, hdrs, _ = await get(f"/recordings/{name}", {"Range": "bytes=0-0"})
        total = int(hdrs.get("Content-Range", "").rsplit("/", 1)[-1])
        check("open-ended range reports total size", total > FRAME_BYTES, str(total))

        code, hdrs, body = await get(f"/recordings/{name}", {"Range": "bytes=0-575"})
        check("range request returns 206", code == 206, str(code))
        check("range request returns exactly 576 bytes", len(body) == 576, str(len(body)))
        check(
            "Content-Range is correct",
            hdrs.get("Content-Range") == f"bytes 0-575/{total}",
            hdrs.get("Content-Range", ""),
        )

        code, hdrs, body = await get(f"/recordings/{name}", {"Range": "bytes=-1024"})
        check("suffix range returns 1024 bytes", code == 206 and len(body) == 1024, f"{code}/{len(body)}")

        code, _, _ = await get(f"/recordings/{name}", {"Range": f"bytes={total + 10}-"})
        check("unsatisfiable range returns 416", code == 416, str(code))

        code, _, _ = await get("/recordings/..%2f..%2fwindows%2fwin.ini")
        check("path traversal blocked", code in (400, 404), str(code))
        code, _, _ = await get("/recordings/nope.mp3")
        check("missing recording 404s", code == 404, str(code))

        print("\n[8] live stream")
        status, hdrs, body = await get("/stream?seconds=2")
        check("/stream returns 200", status == 200, str(status))
        check("/stream is chunked", hdrs.get("Transfer-Encoding") == "chunked", hdrs.get("Transfer-Encoding", ""))
        check("/stream is audio/mpeg", hdrs.get("Content-Type") == "audio/mpeg", hdrs.get("Content-Type", ""))
        check("/stream carried audio", len(body) > FRAME_BYTES, f"{len(body)} bytes")
        check("/streamed data is valid MP3", frames_valid(body), f"{len(body)} bytes")

        t0 = time.monotonic()
        await get("/stream?seconds=1")
        elapsed = time.monotonic() - t0
        check("/stream?seconds=N honours its duration", elapsed < 6, f"{elapsed:.1f}s")

        code, _, _ = await get("/stream?seconds=0")
        check("/stream?seconds=0 rejected", code == 400, str(code))

        print("\n[9] abrupt device death and reconnect")
        dev.kill()
        dev.wait()
        check("link notices the device vanished", await wait_for(lambda: not link.connected, 10, "link drop"))
        code, _, _ = await get("/healthz")
        check("/healthz flips to 503", code == 503, str(code))
        check("ring survives the drop", (await get("/audio?seconds=2"))[0] in (200, 503))
        check("recording finalised on disconnect", (tmp / "rec" / name).stat().st_size > 0)

        print("\n[10] reconnect loop survives and reattaches")
        dev2 = subprocess.Popen(
            [sys.executable, str(HERE / "fake_device.py"), "--port", str(DEVICE_PORT), "--speed", "4"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            check("reconnects to a restarted device", await wait_for(lambda: link.connected, 20, "reconnect"))
            check("session counter advanced", link.sessions >= 2, str(link.sessions))
            check("/healthz recovers to 200", await wait_for_get("/healthz", 200, 10, "healthz 200"))
            check(
                "fresh audio retrievable after reconnect",
                await wait_for_get("/audio?seconds=2", 200, 10, "fresh window"),
            )
            check("a second recording was opened", len(list((tmp / "rec").glob("*.mp3"))) >= 2)
        finally:
            dev2.kill()
            dev2.wait()

        print("\n[11] stream terminates instead of hanging when the device is gone")
        t0 = time.monotonic()
        status, _, _ = await get("/stream")
        elapsed = time.monotonic() - t0
        check("idle /stream ends on its own", status == 200 and elapsed < 20, f"{status} in {elapsed:.1f}s")

    finally:
        link.close()
        link_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await link_task
        await svc.close()


def main() -> int:
    print(f"Xapyra retrieval server checks\npython {sys.version.split()[0]}")
    print("\n[0] --file filename guard")
    for name, ok, detail in test_safe_output_name():
        check(name, ok, detail)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dev = subprocess.Popen(
            [sys.executable, str(HERE / "fake_device.py"), "--port", str(DEVICE_PORT), "--speed", "4"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.5)
            asyncio.run(scenario(tmp, dev))
        finally:
            dev.kill()
            dev.wait()

    total = passed + len(failed)
    print(f"\n{passed}/{total} checks passed")
    for f in failed:
        print(f"  failed: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
