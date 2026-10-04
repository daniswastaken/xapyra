"""
Main interactive shell -- the core loop of Xapyra.

Mirrors betterVpack's app.py: a COMMANDS table drives `help`, every command
is a module under src/commands, and CancelCommand/KeyboardInterrupt unwind
gracefully back to the chatbox instead of killing the session.
"""
from src import ui
from src.commands import (
    doctor,
    list as recordings,
    record,
    retrieve,
    scan,
    status,
    stop,
    timings,
)
from src.engine import Engine


COMMANDS = [
    ("scan",     "Find the ESP32 on the hotspot"),
    ("record",   "Start capturing audio (guided)"),
    ("stop",     "Stop capture and show the 1:1 report"),
    ("status",   "Link, ring and recording state"),
    ("timings",  "Wall vs audio vs gap report"),
    ("retrieve", "Save the last N seconds to a file"),
    ("list",     "Saved recordings"),
    ("doctor",   "Check python, ports and device reachability"),
    ("clear",    "Clear the terminal screen"),
    ("help",     "Show this help message"),
    ("exit",     "Exit Xapyra"),
]


class Ctx:
    """Shared shell state: one engine plus its last known config."""

    def __init__(self) -> None:
        self.engine = Engine()
        self.last_scan: list[str] = []
        self.http_port = 8081
        self.outdir = "recordings"


def _show_help():
    """
    Renders the dynamic help table using the TUI grid system.
    Relies on pre-defined COMMANDS tuple to maintain order and consistent
    formatting across the shell without hardcoding spacing logic.
    """
    ui.header("Available Commands")
    ui.show_command_table(COMMANDS)
    print()


def _show_shortcuts():
    """
    Exposes supported keyboard intercepts.
    Standard Windows CMD/PS buffers cannot easily trap Esc or arrow keys
    without raw key handling, so line input plus Ctrl+C is the contract --
    same constraint the betterVpack shell documents.
    """
    shortcuts = [
        ("?", "Show this shortcuts message"),
        ("Ctrl+C", "Cancel the current command"),
    ]
    ui.header("Keyboard Shortcuts")
    ui.show_command_table(shortcuts)
    print()


def _chat_status(ctx: Ctx) -> dict:
    """Bottom status row for the chatbox: device / link / rec / 1:1."""
    if not ctx.engine.running:
        return {"h1": "device", "v1": "none", "h2": "link", "v2": "idle",
                "h3": "rec", "v3": "idle", "h4": "1:1", "v4": "--"}
    st = ctx.engine.status()
    dev = st["device"]
    rec = st.get("recording", {})
    t = st.get("timing", {})
    live = "LIVE" if dev.get("connected") else "DOWN"
    rname = rec.get("path") if rec.get("active") else "idle"
    drift = t.get("drift_seconds", 0.0) or 0.0
    return {
        "h1": "device", "v1": f"{dev.get('host', '?')}:{dev.get('port', '?')}",
        "h2": "link", "v2": live,
        "h3": "rec", "v3": str(rname),
        "h4": "1:1", "v4": f"{drift:+.1f}s",
    }


COMMAND_RUNNERS = {
    "scan": scan.run,
    "record": record.run,
    "stop": stop.run,
    "status": status.run,
    "timings": timings.run,
    "retrieve": retrieve.run,
    "list": recordings.run,
    "doctor": doctor.run,
}


def handle(line: str, ctx: Ctx) -> bool:
    """Dispatch one input line. Returns False when the shell should exit."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("exit", "quit", "q"):
        ui.stop_live_status()
        if ctx.engine.running:
            ctx.engine.stop()
        ui.info("Goodbye!")
        ui.reset_tui()
        return False
    if cmd == "?":
        _show_shortcuts()
        return True
    if cmd == "help":
        _show_help()
        return True
    if cmd in ("clear", "cls"):
        ui.setup_tui()
        return True
    runner = COMMAND_RUNNERS.get(cmd)
    if runner is None:
        ui.warn(f"Unknown command: '{cmd}'. Type 'help' for available commands.")
        return True
    try:
        runner(ctx, args)
    except (ui.CancelCommand, KeyboardInterrupt):
        print()
        ui.warn("Command cancelled.")
    return True


def start(simple: bool = False):
    """
    Main REPL loop. Dispatches commands while maintaining the TUI layout.
    Catches CancelCommand natively to allow graceful aborts (Ctrl+C) during
    long-running workflows without blowing up the shell.

    `simple` (or a non-TTY stdin) selects the linear fallback: no scroll
    regions, no absolute cursor moves -- just scrolling text and a `> `
    prompt. Same commands, renders on any terminal, including ones that
    ignore ANSI positioning (where the fullscreen chatbox would draw blank).
    """
    import sys
    import traceback

    if simple or not sys.stdin.isatty():
        if not simple:
            ui.warn("stdin is not interactive -- linear mode (same commands, no chatbox).")
        _linear_loop()
        return
    try:
        ui.setup_tui()
        ctx = Ctx()
        ui.start_live_status(lambda: _chat_status(ctx))
        ui.info("Xapyra shell -- type 'help'. 'scan' finds the ESP32, 'record' captures.")

        while True:
            try:
                cmd = ui.prompt_shell(status=_chat_status(ctx))
            except (ui.CancelCommand, KeyboardInterrupt):
                print()
                ui.warn("Command cancelled.")
                continue
            if not handle(cmd, ctx):
                break
    except Exception:
        # Never leave the user's terminal windowed to a broken scroll region,
        # and never die silent: the traceback is the diagnosis.
        try:
            ui.stop_live_status()
            ui.reset_tui()
        finally:
            traceback.print_exc()
        raise SystemExit(1)
    finally:
        ui.stop_live_status()


def _linear_loop():
    """Fallback REPL: plain scrolling I/O for terminals without ANSI support."""
    ui.header("XAPYRA retrieval shell (linear mode)")
    ctx = Ctx()
    ui.info("Type 'help'. 'scan' finds the ESP32, 'record' captures.")
    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            cmd = "exit"
        if not handle_linear(cmd, ctx):
            break


def handle_linear(line: str, ctx: Ctx) -> bool:
    """Dispatch without touching scroll regions (exit path skips reset_tui)."""
    if line.strip().lower() in ("exit", "quit", "q"):
        if ctx.engine.running:
            ctx.engine.stop()
        ui.info("Goodbye!")
        return False
    return handle(line, ctx)
