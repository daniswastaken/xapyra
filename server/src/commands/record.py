"""Start a capture run: pick device, pick output file, spin up the engine."""
import asyncio
from datetime import datetime
from pathlib import Path

from src import ui
from src.cli import safe_output_name
from src.engine import Config
from src.net.discover import DEFAULT_PORT, discover, probe


def _fresh_devices(ctx) -> list[str]:
    """Cached last_scan goes stale across device reboots -- DHCP hands out a
    new IP (e.g. after flashing, or a new hostname like Xapyra-o3). Re-probe
    the cached host(s); silently rescan when they're gone so `record` never
    dials a dead IP."""
    cached = list(ctx.last_scan or [])
    if cached and all(asyncio.run(probe(h, DEFAULT_PORT, 1.0)) for h in cached):
        return cached
    found = asyncio.run(discover(DEFAULT_PORT))
    ctx.last_scan = found
    if found != cached:
        ui.info("Device IP changed since the last scan -- refreshed.")
    return found


def _pick_host(ctx) -> str | None:
    found = _fresh_devices(ctx)
    if len(found) == 1:
        ui.info(f"Using {found[0]} (verified live just now).")
        if ui.prompt_confirm("Record from here?", default=True):
            return found[0]
    elif len(found) > 1:
        picked = ui.prompt_choice("Which device?", found + ["Type a different IP"])
        if picked != "Type a different IP":
            return picked
    return ui.prompt("ESP32 IP (as printed on its serial monitor)").strip() or None


def run(ctx, args: list[str]) -> None:
    if ctx.engine.running:
        ui.warn("Already recording -- 'stop' first.")
        return
    ui.header("Record")

    host = _pick_host(ctx)
    if not host:
        ui.warn("No device picked.")
        return

    mode = ui.prompt_choice("Output", [
        "One file for the whole run (recommended)",
        "A new file every session",
    ])
    filename: str | None = None
    if mode.startswith("One file"):
        default = f"xapyra-{datetime.now():%Y%m%d-%H%M%S}.mp3"
        while True:
            name = ui.prompt("Output filename", default=default).strip()
            try:
                filename = safe_output_name(name)
                break
            except ValueError as exc:
                ui.error(str(exc))

    cfg = Config(
        device_host=host,
        http_port=ctx.http_port,
        outdir=Path(ctx.outdir),
        filename=filename,
    )
    port = ctx.engine.start(cfg)
    ctx.http_port = port
    # Don't declare victory before the link exists: the chatbox row reads
    # DOWN/idle until the first accept, which misleads for ~1s after record.
    if _wait_for_link(ctx, timeout=10.0):
        ui.success("Recording -- green LED should be blinking now.")
    else:
        ui.warn("Engine up but the device hasn't accepted yet -- watch the link row.")
    ui.info(f"Dashboard: http://localhost:{port}/   Stop with 'stop'.")


def _wait_for_link(ctx, timeout: float) -> bool:
    """Poll the engine until the device accepts (or the timeout expires)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ctx.engine.status().get("device", {}).get("connected"):
                return True
        except Exception:
            return False
        time.sleep(0.2)
    return False
