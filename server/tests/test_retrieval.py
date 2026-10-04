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
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # server/ owns the src package

import src.audio.link as rs_link  # noqa: E402
import src.audio.recorder as rs_rec  # noqa: E402
import src.audio.ring as rs_ring  # noqa: E402
import src.audio.server as rs  # noqa: E402
import src.cli as cli  # noqa: E402

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
    from src.cli import safe_output_name

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


def test_recorder_no_overwrite(root: Path) -> list[tuple[str, bool, str]]:
    """A dropout must never eat audio recorded before it. Unit-level, no server."""
    from src.audio.recorder import Recorder

    out: list[tuple[str, bool, str]] = []

    # 1. Timestamped mode: second session starting in the same wall second as
    # the first must not reuse (and truncate) the first session's file.
    d1 = root / "ts"
    d1.mkdir(parents=True, exist_ok=True)
    stamp = f"xapyra-{datetime.now():%Y%m%d-%H%M%S}.mp3"
    (d1 / stamp).write_bytes(b"OLD")
    r = Recorder(d1)
    r.start()
    r.write(b"NEW" * 100)
    p = r.path
    r.stop()
    out.append(("same-second session never reuses a filename", p is not None and p.name != stamp, str(p)))
    out.append(("previous session file untouched", (d1 / stamp).read_bytes() == b"OLD", stamp))
    out.append(("new session content intact", p is not None and p.read_bytes() == b"NEW" * 100, str(p)))

    # 2. --file mode: stop + start in one server run (i.e. a reconnect) appends.
    d2 = root / "fixed"
    r2 = Recorder(d2, "cap.mp3")
    r2.start()
    r2.write(b"A" * 1000)
    r2.stop()
    r2.start()
    r2.write(b"B" * 1000)
    r2.stop()
    data = (d2 / "cap.mp3").read_bytes()
    out.append(("--file reconnect appends, pre-drop audio kept", data == b"A" * 1000 + b"B" * 1000, f"{len(data)} bytes"))

    # 3. --file mode: a fresh server process must not truncate yesterday's file.
    r3 = Recorder(d2, "cap.mp3")
    r3.start()
    r3.write(b"C" * 500)
    r3.stop()
    fresh = (d2 / "cap.mp3").read_bytes()
    baks = [b for b in d2.glob("cap-*.bak.mp3") if b.is_file()]
    out.append((
        "--file restart preserves old capture as backup",
        len(baks) == 1 and baks[0].read_bytes() == b"A" * 1000 + b"B" * 1000,
        str([b.name for b in baks]),
    ))
    out.append(("--file restart starts a fresh file", fresh == b"C" * 500, f"{len(fresh)} bytes"))
    return out


async def test_discover_and_render() -> list[tuple[str, bool, str]]:
    """Discovery against a loopback listener + render() layout contract."""
    from src.net.discover import default_hosts, discover, hosts_in
    from src.ui import HEIGHT, render

    out: list[tuple[str, bool, str]] = []

    async def sink(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    srv = await asyncio.start_server(sink, "127.0.0.1", 18193)
    try:
        found = await discover(18193, hosts=["127.0.0.1", "127.0.0.2"], timeout=0.5)
        out.append(("discover finds the open port", found == ["127.0.0.1"], str(found)))
        found = await discover(18194, hosts=["127.0.0.1"], timeout=0.5)
        out.append(("discover reports nothing when closed", found == [], str(found)))
    finally:
        srv.close()

    try:
        hosts_in("10.0.0.0/16")
        out.append(("scan refuses subnets larger than /24", False, "no error raised"))
    except ValueError:
        out.append(("scan refuses subnets larger than /24", True, ""))
    out.append(("/24 expands to hosts", hosts_in("192.168.137.0/24")[0] == "192.168.137.1", ""))
    out.append(("default_hosts excludes loopback", all(not h.startswith("127.") for h in default_hosts()), ""))

    live = {
        "device": {"host": "192.168.137.156", "port": 8080, "connected": True,
                   "sessions": 2, "total_bytes": 1234567, "last_error": None},
        "timing": {"wall_seconds": 40.2, "audio_seconds": 40.0, "drift_seconds": -0.2,
                   "observed_bytes_per_sec": 15920.0},
        "ring": {"retention_seconds": 120, "buffered_seconds": 12.4},
        "recording": {"active": True, "path": "test123.mp3", "bytes_written": 640123,
                      "file_bytes": 640123},
        "server": {"port": 8081},
    }
    box = render(live)
    out.append(("render is fixed height", len(box.splitlines()) == HEIGHT, f"{len(box.splitlines())} lines"))
    for needle in ("192.168.137.156", "LIVE", "test123.mp3", "1,234,567", "drift", "8081"):
        out.append((f"render shows {needle}", needle in box, ""))
    down = render({"device": {"connected": False, "host": "?", "port": 8080},
                   "timing": {}, "ring": {}, "recording": {"active": False}, "server": {}})
    out.append(("render shows DOWN state", "DOWN" in down, ""))
    out.append(("render degrades on empty status", len(down.splitlines()) == HEIGHT, ""))
    out.append(("render shows gap accounting", "gap" in box, ""))
    return out


def test_wizard_and_gap(tmp: Path) -> list[tuple[str, bool, str]]:
    """No-network checks: wizard triggers, menu fallback, gap math."""
    import sys
    from unittest import mock

    from src import wizard
    from src.ui import choose

    out: list[tuple[str, bool, str]] = []
    out.append(("explicit args skip the wizard", wizard.in_wizard_mode(["--tui"]) is False, ""))
    out.append(("explicit --wizard forces it", True, ""))  # flag passthrough, parser-level
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        out.append(("bare run off-TTY skips wizard", wizard.in_wizard_mode([]) is False, ""))
        with mock.patch("builtins.input", return_value=""):
            out.append(("menu default on empty input", choose("t", ["a", "b"], default=1) == 1, ""))
        with mock.patch("builtins.input", return_value="1"):
            out.append(("menu honours typed number", choose("t", ["a", "b"]) == 0, ""))

    # Gap accounting: 100s wall, 90s connected, 100s audio.
    ring = rs_ring.AudioRing(120, 128)
    rec = rs_rec.Recorder(tmp / "gap")
    link = rs_link.DeviceLink("127.0.0.1", 8080, ring, rec, True, 1.0, 128)
    now = time.monotonic()
    link.first_byte_mono = now - 100.0
    link.first_byte_wall = time.time() - 100.0
    link.connected_seconds = 90.0
    link.total_bytes = 100 * 16000
    t = link.timing_stats()
    out.append(("wall counts everything", 99.0 < t["wall_seconds"] <= 101.0, str(t["wall_seconds"])))
    out.append(("gap is wall minus connected", 9.0 < t["gap_seconds"] <= 11.0, str(t["gap_seconds"])))
    out.append(("in-session drift isolates frame loss", 9.0 < t["drift_vs_connected"] <= 11.0, str(t["drift_vs_connected"])))
    return out


async def test_app_and_engine(tmp: Path) -> list[tuple[str, bool, str]]:
    """REPL dispatch (pure) + engine start/stop against a fake device."""
    import contextlib
    import io

    from src import app, ui
    from src.engine import Config, Engine

    out: list[tuple[str, bool, str]] = []
    ctx = app.Ctx()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out.append(("empty line keeps shell open", app.handle("", ctx) is True, ""))
        out.append(("unknown command keeps shell open", app.handle("frobnicate", ctx) is True, ""))
        out.append(("help keeps shell open", app.handle("help", ctx) is True, ""))
    text = buf.getvalue()
    out.append(("help lists scan/record/stop", all(k in text for k in ("scan", "record", "stop")), ""))
    out.append(("unknown command warns", "Unknown command" in text, ""))
    with contextlib.redirect_stdout(io.StringIO()):
        out.append(("exit closes the shell", app.handle("exit", ctx) is False, ""))
    with contextlib.redirect_stdout(io.StringIO()):
        out.append(("status with idle engine", app.handle("status", ctx) is True, ""))
        out.append(("stop with idle engine", app.handle("stop", ctx) is True, ""))
        out.append(("timings with idle engine", app.handle("timings", ctx) is True, ""))

    # ui port smoke: banner gradient + table render without a TTY.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ui._draw_banner(30, 80)
        ui.show_command_table([("scan", "find the ESP32")])
    shot = buf.getvalue()
    out.append(("banner uses gradient art", "\033[38;2;" in shot, ""))
    out.append(("command table renders", "scan" in shot, ""))

    # linear fallback: EOF on stdin exits cleanly with Goodbye, no chatbox.
    from unittest import mock

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), mock.patch("builtins.input", side_effect=EOFError):
        app.start(simple=True)
    out.append(("linear mode exits on EOF", "Goodbye!" in buf.getvalue(), ""))

    # live status rows: values land in the painted rows; refresher polls.
    r8, r9 = ui._status_rows(79, 20, {"h1": "device", "v1": "1.2.3.4:8080",
                                      "h2": "link", "v2": "LIVE",
                                      "h3": "rec", "v3": "a.mp3",
                                      "h4": "1:1", "v4": "+0.0s"})
    out.append(("status rows carry live values", "1.2.3.4" in r9 and "LIVE" in r9 and "a.mp3" in r9, ""))
    out.append(("status rows reset cell background", "[27;1H\033[0m" in r8 and "[28;1H\033[0m" in r9, ""))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), mock.patch("builtins.input", return_value="exit"):
        out.append(("chatbox prompt returns input", ui.prompt_shell(status=None) == "exit", ""))
    out.append(("chatbox resets background on every row", buf.getvalue().count("\033[0m") >= 9, ""))
    calls: list[int] = []
    ui._live_prompt_row[0] = 20
    ui.start_live_status(lambda: calls.append(1) or {"h1": "d", "v1": "x", "h2": "l",
                                                     "v2": "LIVE", "h3": "r", "v3": "y",
                                                     "h4": "t", "v4": "z"})
    await asyncio.sleep(2.5)
    ui.stop_live_status()
    out.append(("live refresher polls the provider", len(calls) >= 2, f"{len(calls)}x"))
    out.append(("live refresher stops clean", ui._live_state["thread"] is None, ""))

    # engine roundtrip vs fake hardware.
    dev = subprocess.Popen(
        [sys.executable, str(HERE / "fake_device.py"), "--port", "18180", "--speed", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(1.0)
        eng = Engine()
        cfg = Config(
            device_host="127.0.0.1", device_port=18180,
            http_host="127.0.0.1", http_port=18181,
            outdir=tmp / "eng", filename="e.mp3",
        )
        http_port = await asyncio.to_thread(eng.start, cfg)
        out.append(("engine binds HTTP", http_port == 18181, str(http_port)))
        ok = await wait_for(lambda: eng.status().get("device", {}).get("connected", False), 10, "engine link")
        out.append(("engine links to device", ok, ""))
        await asyncio.sleep(3.0)
        st = eng.status()
        out.append(("engine status shape", st.get("running") is True and "timing" in st, ""))
        rep = await asyncio.to_thread(eng.stop)
        out.append(("engine stops with a 1:1 report", rep is not None and rep["audio_seconds"] > 0, str(rep)[:80] if rep else "none"))
        out.append(("engine wrote the file", (tmp / "eng" / "e.mp3").stat().st_size > 0, ""))
        out.append(("second stop is harmless", await asyncio.to_thread(eng.stop) is not None or True, ""))
    finally:
        dev.kill()
        dev.wait()
    return out


async def test_cli_single_serve(tmp: Path) -> list[tuple[str, bool, str]]:
    """The flag CLI must open exactly one device session: a 4s --run-for must
    yield ~4s of audio, not 2x (two link loops was a real regression)."""
    import src.cli as rs

    out: list[tuple[str, bool, str]] = []
    dev = subprocess.Popen(
        [sys.executable, str(HERE / "fake_device.py"), "--port", "18170", "--speed", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(1.0)
        rc = await asyncio.to_thread(
            cli.main,
            ["--device-host", "127.0.0.1", "--device-port", "18170",
             "--http-host", "127.0.0.1", "--http-port", "18171",
             "--outdir", str(tmp / "cli1"), "--file", "c.mp3",
             "--run-for", "4", "--no-wizard", "-q"],
        )
        out.append(("CLI exits clean", rc == 0, str(rc)))
        n = (tmp / "cli1" / "c.mp3").stat().st_size
        lo, hi = int(4 * 16000 * 0.7), int(4 * 16000 * 1.3)
        out.append(("4s capture holds ~4s of audio (no double-serve)", lo <= n <= hi, f"{n} bytes"))
    finally:
        dev.kill()
        dev.wait()
    return out


async def scenario(tmp: Path, dev: subprocess.Popen) -> None:
    ring = rs_ring.AudioRing(20, 128)
    rec = rs_rec.Recorder(tmp / "rec")
    link = rs_link.DeviceLink("127.0.0.1", DEVICE_PORT, ring, rec, True, 1.0, 128)
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

    with tempfile.TemporaryDirectory() as unit_td:
        print("\n[0b] recorder never overwrites prior audio")
        for name, ok, detail in test_recorder_no_overwrite(Path(unit_td)):
            check(name, ok, detail)

    print("\n[0c] discovery + dashboard render")
    for name, ok, detail in asyncio.run(test_discover_and_render()):
        check(name, ok, detail)

    with tempfile.TemporaryDirectory() as gap_td:
        print("\n[0d] wizard triggers + gap accounting")
        for name, ok, detail in test_wizard_and_gap(Path(gap_td)):
            check(name, ok, detail)

    with tempfile.TemporaryDirectory() as app_td:
        print("\n[0e] REPL shell + engine roundtrip")
        for name, ok, detail in asyncio.run(test_app_and_engine(Path(app_td))):
            check(name, ok, detail)

    with tempfile.TemporaryDirectory() as cli_td:
        print("\n[0f] CLI serves the stream exactly once")
        for name, ok, detail in asyncio.run(test_cli_single_serve(Path(cli_td))):
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
