#!/usr/bin/env python3
"""No-args setup wizard: scan, pick, record. betterVpack-style messages,
Gemini-CLI-style arrow menus, zero CLI flags required.

Returns a plain dict that main() folds into argparse args, so every choice
stays overridable from the command line afterwards.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.net.discover import DEFAULT_PORT, discover, local_ipv4s
from src.ui import BOLD, RESET, PickerAbort, ask, choose, message


def banner() -> None:
    print(f"{BOLD}XAPYRA retrieval -- pick a device, hit record.{RESET}")
    print("ESP32 streams MP3 over TCP :8080, this laptop keeps it as .mp3.")


async def pick_device(port: int, subnets: list[str] | None, timeout: float) -> str:
    """Scan, let the user pick a responder, or fall back to a typed IP."""
    scope = f"subnet(s) {', '.join(subnets)}" if subnets else (
        f"local /24(s) around {', '.join(local_ipv4s()) or 'no NIC found'}"
    )
    message(f"scanning {scope} for :{port} ...", 1)
    found = await discover(port, subnets=subnets, timeout=timeout)
    if not found:
        message(f"nothing answering on :{port}.", 4)
        action = choose("No device found -- what now?", ["Scan again", "Type the IP by hand", "Quit"])
        if action == 0:
            return await pick_device(port, subnets, timeout)
        if action == 1:
            return ask("ESP32 IP (as printed on its serial monitor)")
        raise PickerAbort
    if len(found) == 1:
        message(f"ESP32 found at {found[0]}", 2)
        action = choose(f"Use {found[0]}?", ["Yes, record from here", "Type a different IP", "Scan again"])
        if action == 0:
            return found[0]
        if action == 1:
            return ask("ESP32 IP")
        return await pick_device(port, subnets, timeout)
    message(f"{len(found)} responders: {', '.join(found)}", 3)
    idx = choose("Which one is the Xapyra?", found + ["Type a different IP", "Scan again"])
    if idx < len(found):
        return found[idx]
    if idx == len(found):
        return ask("ESP32 IP")
    return await pick_device(port, subnets, timeout)


def pick_output(outdir: Path) -> str | None:
    """One fixed file (recommended) or a new file per session. None = per-session."""
    mode = choose("Where should the audio go?", [
        "One file for the whole run (recommended)",
        "A new file every session",
    ])
    if mode == 1:
        return None
    default = f"xapyra-{datetime.now():%Y%m%d-%H%M%S}.mp3"
    while True:
        name = ask("Output filename inside " + str(outdir), default)
        name = name.strip()
        if not name:
            message("filename must not be empty", 4)
            continue
        if "/" in name or "\\" in name:
            message("plain filename, not a path", 4)
            continue
        return name if name.endswith(".mp3") else name + ".mp3"


async def run_wizard(port: int = DEFAULT_PORT, subnets: list[str] | None = None,
                     timeout: float = 0.4, outdir: str = "recordings") -> dict:
    """Full interactive flow. Raises PickerAbort / KeyboardInterrupt on quit."""
    banner()
    host = await pick_device(port, subnets, timeout)
    if not host:
        raise PickerAbort
    out = Path(outdir)
    filename = pick_output(out)
    message(f"device {host}:{port}", 1)
    if filename:
        message(f"recording the whole run into {out / filename}", 1)
    else:
        message(f"recording each session into {out}/xapyra-<timestamp>.mp3", 1)
    go = choose("Start recording?", ["Yes -- green LED should start blinking", "No, quit"])
    if go != 0:
        raise PickerAbort
    return {"device_host": host, "filename": filename, "outdir": outdir}


def in_wizard_mode(args: list[str]) -> bool:
    """No CLI args on a TTY means the wizard, not silent defaults."""
    import sys

    return len(args) == 0 and sys.stdin.isatty()
