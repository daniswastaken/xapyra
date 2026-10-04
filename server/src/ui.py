"""
Terminal UI helpers -- colors, banners, prompts, formatting.
Pure stdlib, no external dependencies.

Ported from betterVpack (github.com/daniswastaken/betterVpack,
refactor/modular-architecture): same scroll-region chatbox, prompt helpers and
command tables, rebranded for Xapyra. Live dashboard box and arrow-key picker
are Xapyra additions in the same visual language.
"""
import asyncio
import io
import os
import sys
import shutil
import threading

# Force UTF-8 on Windows so box-drawing / emoji don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )

# ── ANSI Color Codes ──────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
ITALIC  = "\033[3m"

RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"

BG_RED     = "\033[41m"
BG_GREEN   = "\033[42m"
BG_BLUE    = "\033[44m"
BG_MAGENTA = "\033[45m"
BG_CYAN    = "\033[46m"

# -- Icons (ASCII-safe for Windows cp1252 compat) -----------------
ICON_SUCCESS = f"{GREEN}+{RESET}"
ICON_ERROR   = f"{RED}x{RESET}"
ICON_WARN    = f"{YELLOW}!{RESET}"
ICON_INFO    = f"{CYAN}>{RESET}"
ICON_ARROW   = f"{MAGENTA}>{RESET}"
ICON_PACK    = f"{CYAN}[pack]{RESET}"
ICON_FOLDER  = f"{YELLOW}[dir]{RESET}"
ICON_GIT     = f"{RED}[git]{RESET}"


def get_terminal_width():
    """
    Dynamically computes terminal width to ensure UI elements (like the chatbox
    and headers) don't wrap unexpectedly, causing layout breaks on resize.
    Falls back to 80 cols safely.
    """
    return shutil.get_terminal_size((80, 24)).columns


def clear_screen():
    """Clear the terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def _apply_gradient(text):
    """Apply a pink-to-cyan horizontal gradient to the ASCII art."""
    lines = text.splitlines()
    if not lines:
        return text
    
    max_len = max(len(line) for line in lines)
    if max_len == 0:
        return text
        
    # Pink (255, 105, 180) to Cyan (0, 255, 255)
    r1, g1, b1 = 255, 105, 180
    r2, g2, b2 = 0, 255, 255
    
    result = []
    for line in lines:
        colored_line = ""
        for i, char in enumerate(line):
            # Calculate gradient ratio for this column
            ratio = i / max_len if max_len > 0 else 0
            
            # Interpolate colors
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            
            colored_line += f"\033[38;2;{r};{g};{b}m{char}"
        colored_line += f"{RESET}"
        result.append(colored_line)
        
    return "\n".join(result)


def _draw_banner(h, w):
    """
    Renders the responsive Vpack branding based on available vertical space.
    Calculates the exact line where the scrolling history should start so the
    Vpack logo remains anchored at the top of the viewport.
    """
    if h >= 28:
        # Full banner for tall terminals
        art = (
            "██   ██  █████  ██████  ██   ██  ██████   █████ \n"
            " ██ ██  ██   ██ ██   ██  ██ ██   ██   ██ ██   ██\n"
            "   █    ███████ ██████    ███    ██████  ███████\n"
            " ██ ██  ██   ██ ██       ██ ██   ██   ██ ██   ██\n"
            "██   ██ ██   ██ ██      ██   ██  ██   ██ ██   ██ "
        )
        print(f"\n{_apply_gradient(art)}\n")
        scroll_start = 10
    else:
        # Mini banner for short terminals
        print(f"  {BOLD}{CYAN}> XAPYRA{RESET}\n")
        scroll_start = 5

    print(f"{DIM}{'─' * (w - 1)}{RESET}\n")
    
    return scroll_start


def separator(char="-", color=DIM):
    """
    Draws a full-width delimiter that dynamically bounds to the active terminal size.
    Prevents text bleed between logically distinct sections.
    """
    w = get_terminal_width()
    print(f"{color}{char * (w - 1)}{RESET}")


def header(text):
    """Print a section header."""
    print(f"\n  {BOLD}{CYAN}{text}{RESET}")
    separator()


def success(text):
    """Print a success message."""
    print(f"  {ICON_SUCCESS} {GREEN}{text}{RESET}")


def error(text):
    """Print an error message."""
    print(f"  {ICON_ERROR} {RED}{text}{RESET}")


def warn(text):
    """Print a warning message."""
    print(f"  {ICON_WARN} {YELLOW}{text}{RESET}")


class CancelCommand(Exception):
    """Raised when the user cancels a prompt via Ctrl+C."""
    pass


def info(text):
    """Print an info message."""
    print(f"  {ICON_INFO} {text}")


def step(text):
    """Print a step/action indicator."""
    print(f"  {ICON_ARROW} {text}")


def prompt(label, default=None):
    """
    Synchronous blocking input that safely intercepts SIGINT (Ctrl+C).
    Instead of allowing KeyboardInterrupt to crash the python interpreter, it raises
    our custom CancelCommand to cleanly unwind the stack back to the main loop.
    """
    if default is not None:
        hint = f" {DIM}({default}){RESET}"
    else:
        hint = ""
    
    try:
        value = input(f"  {BOLD}{CYAN}?{RESET} {label}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise CancelCommand()
    
    print(f"\033[{_scroll_end};1H\n", end="", flush=True)
    if value == "" and default is not None:
        return default
    return value


def prompt_confirm(label, default=True):
    """
    Ask the user a yes/no question. Returns True/False.
    """
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"  {BOLD}{CYAN}?{RESET} {label} {DIM}({hint}){RESET}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise CancelCommand()
    
    print(f"\033[{_scroll_end};1H\n", end="", flush=True)
    if value == "":
        return default
    return value in ("y", "yes")


def prompt_choice(label, options, default=0):
    """
    Show a numbered menu and let the user pick.
    options: list of strings.
    default: index of the default choice (0-based).
    Returns the selected string.
    """
    print(f"\n  {BOLD}{CYAN}?{RESET} {label}")
    for i, opt in enumerate(options):
        marker = f"{CYAN}*{RESET}" if i == default else f"{DIM}o{RESET}"
        default_tag = f" {DIM}(default){RESET}" if i == default else ""
        print(f"    {marker} {BOLD}{i + 1}{RESET}. {opt}{default_tag}")
    
    try:
        print(f"  {DIM}Enter choice [1-{len(options)}]{RESET}: ", end="", flush=True)
        value = input("").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise CancelCommand()
    
    print(f"\033[{_scroll_end};1H\n", end="", flush=True)
    if value == "":
        return options[default]
    
    try:
        idx = int(value) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        pass
    
    warn(f"Invalid choice, using default: {options[default]}")
    return options[default]


def _get_workspace_name():
    """Get current working directory name for the status bar."""
    return os.path.basename(os.getcwd())


def _get_git_branch():
    """Get the current git branch name, or None."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# Module-level scroll region state
_scroll_start = 18
_scroll_end = 20


def setup_tui():
    """
    Bootstraps the ANSI scrolling environment. 
    By defining explicit \033[T;Br scroll regions, we lock the top banner in place 
    while isolating all command output to the middle chunk of the terminal window, 
    preventing the chatbox from being overwritten.
    """
    global _scroll_start, _scroll_end
    # Nuke the entire screen AND scrollback buffer to prevent history bleed
    print("\033[2J\033[3J\033[H", end="")
    
    h = shutil.get_terminal_size((80, 24)).lines
    w = get_terminal_width()
    
    _scroll_start = _draw_banner(h, w)
    
    # Set scroll region from below banner to above prompt
    _scroll_end = max(_scroll_start + 2, h - 9)
    print(f"\033[{_scroll_start};{_scroll_end}r", end="")
    
    # Move cursor to the bottom of the scroll region
    print(f"\033[{_scroll_end};1H", end="")


def enter_scroll():
    """
    Repositions the hardware cursor back into the active scrolling zone.
    Must be called after rendering the chatbox (which lives in the static bottom pane)
    so standard prints flow naturally into the history area.
    """
    print(f"\033[{_scroll_start};{_scroll_end}r", end="")
    print(f"\033[{_scroll_end};1H", end="")


def reset_tui():
    """
    Tears down the ANSI scroll margins before exit.
    Failing to reset this would leave the user's standard cmd/powershell permanently 
    windowed to the middle of the screen.
    """
    h = shutil.get_terminal_size((80, 24)).lines
    # Reset scroll margins and move cursor to the very bottom
    print(f"\033[r\033[{h};1H", end="")


def prompt_shell(status=None):
    """
    Renders the persistent chatbox interface at the bottom of the screen.
    Uses absolute positioning (\\033[Y;XH) to paint the UI grid without shifting
    the terminal's native history buffer.

    `status` optionally overrides the bottom status row, e.g. the Xapyra app
    passes device/link/rec/1:1 strings so the chatbox doubles as a monitor:
    {"h1": "device", "v1": ..., "h2": "link", "v2": ...,
     "h3": "rec", "v3": ..., "h4": "1:1", "v4": ...}.
    """
    w = get_terminal_width()
    safe_w = w - 1  # Prevent scrollbar wrapping on Windows for text
    h = shutil.get_terminal_size((80, 24)).lines
    
    # Calculate the fixed prompt area (bottom 9 lines)
    prompt_row = max(16, h - 8)
    
    # ANSI clear to end of line
    clear_eol = "\033[K"
    
    # Row 1: ? for shortcuts (right-aligned)
    shortcuts = "? for shortcuts"
    gap0 = max(1, safe_w - len(shortcuts))
    r1 = f"\033[{prompt_row};1H\033[0m{' ' * gap0}{DIM}{shortcuts}{RESET}"

    # Row 2: Separator line
    r2 = f"\033[{prompt_row + 1};1H\033[0m{DIM}{'─' * safe_w}{RESET}"

    # Row 3: Top bar
    top_left = "Type 'help' for commands"
    r3 = f"\033[{prompt_row + 2};1H\033[0m{DIM}{top_left}{RESET}"

    # Row 4: Top padding (lower half-block in grey foreground)
    # This draws a 0.5-line grey bar at the BOTTOM of this row.
    # safe_w, not w: a full-width fill wraps on narrow terminals and the
    # spilled background survives under later rows (grey ghost rows).
    r4 = f"\033[{prompt_row + 3};1H\033[0m\033[38;5;237m{'▄' * safe_w}{RESET}"

    # Row 5: Center text input (full grey background)
    bg = "\033[48;5;237m"
    r5_bg = f"\033[{prompt_row + 4};1H\033[0m{bg}{' ' * safe_w}{RESET}"

    # Row 6: Bottom padding (upper half-block in grey foreground)
    # This draws a 0.5-line grey bar at the TOP of this row
    r6 = f"\033[{prompt_row + 5};1H\033[0m\033[38;5;237m{'▀' * safe_w}{RESET}"

    # Row 7: Empty breathing line below the chatbox
    r7 = f"\033[{prompt_row + 6};1H\033[0m{clear_eol}"
    
    # Row 8 & 9: Tabular status (extracted so the live refresher can repaint
    # just these two rows without redrawing the whole chatbox)
    r8, r9 = _status_rows(safe_w, prompt_row, status)

    # Draw all rows WITHOUT newlines to prevent terminal scrolling bugs!
    with _live_lock:
        for row in (r1, r2, r3, r4, r5_bg, r6, r7, r8, r9):
            print(f"{row}{clear_eol}", end="", flush=True)

        # Move cursor onto the text line of the input bar
        prefix = f"\033[{prompt_row + 4};1H{bg} {MAGENTA}{BOLD}>{RESET}{bg} "
        print(f"{prefix}", end="", flush=True)
        _live_prompt_row[0] = prompt_row
    
    try:
        value = input("")
    except (EOFError, KeyboardInterrupt):
        value = "exit"
        print()
    finally:
        print(RESET, end="")
        
    # Re-activate scroll region and position cursor inside it
    enter_scroll()
    
    if value and value != "exit":
        print(f"{DIM}> {value}{RESET}")
        
    return value.strip()


def _status_rows(safe_w, prompt_row, status):
    """Build the two bottom status rows (r8 headers, r9 values).

    Byte-identical to the historical inline version; extracted so the live
    refresher can repaint these rows once a second while input() blocks.
    """
    h1, v1 = "workspace (/directory)", os.getcwd()
    h2, v2 = "branch", _get_git_branch() or "none"
    h3, v3 = "status", "ready"
    h4, v4 = "/engine", "vpack-core"
    if status:
        h1 = status.get("h1", h1)
        v1 = status.get("v1", v1)
        h2 = status.get("h2", h2)
        v2 = status.get("v2", v2)
        h3 = status.get("h3", h3)
        v3 = status.get("v3", v3)
        h4 = status.get("h4", h4)
        v4 = status.get("v4", v4)

    cw1 = int(safe_w * 0.40)
    cw2 = int(safe_w * 0.20)
    cw3 = int(safe_w * 0.20)
    cw4 = safe_w - cw1 - cw2 - cw3

    if len(v1) > cw1 - 2:
        v1 = "..." + v1[-(cw1 - 5):]

    # Every row opens with a full reset: SGR foreground codes leave the cell
    # background untouched, so without this any spilled input-bar grey
    # survives underneath later text (ghost background on the status rows).
    r8 = f"\033[{prompt_row + 7};1H\033[0m{DIM}{h1:<{cw1}}{h2:<{cw2}}{h3:<{cw3}}{h4:<{cw4}}{RESET}"

    v3_colored = f"{MAGENTA}{BOLD}{v3:<{cw3}}{RESET}"
    v4_colored = f"{WHITE}{BOLD}{v4:<{cw4}}{RESET}"
    r9 = f"\033[{prompt_row + 8};1H\033[0m{WHITE}{BOLD}{v1:<{cw1}}{v2:<{cw2}}{RESET}{v3_colored}{v4_colored}"
    return r8, r9


# -- live status refresher -------------------------------------------------- #
# The chatbox snapshot problem: prompt_shell paints the status row once, then
# input() blocks -- so LINK/REC freeze at whatever they were when you last
# pressed Enter. This daemon repaints just rows 8-9 every second (cursor
# saved/restored around the paint, full draw guarded by the same lock), so
# the row is a monitor, not a stale screenshot.
_live_lock = threading.Lock()
_live_prompt_row = [None]
_live_state = {"provider": None, "stop": True, "thread": None, "last": None}


def start_live_status(provider):
    """Repaint rows 8-9 once a second from `provider()` (dict or None)."""
    stop_live_status()
    _live_state["provider"] = provider
    _live_state["stop"] = False
    _live_state["last"] = None
    t = threading.Thread(target=_live_loop, daemon=True, name="xapyra-live-status")
    _live_state["thread"] = t
    t.start()


def stop_live_status():
    _live_state["stop"] = True
    t = _live_state.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    _live_state["thread"] = None
    _live_state["provider"] = None


def _live_loop():
    import time

    while not _live_state["stop"]:
        time.sleep(1.0)
        provider = _live_state["provider"]
        row = _live_prompt_row[0]
        try:
            status = provider() if provider is not None else None
        except Exception:
            continue
        if status is None or row is None or not sys.stdout.isatty():
            continue
        safe_w = get_terminal_width() - 1
        r8, r9 = _status_rows(safe_w, row, status)
        key = (r8, r9)
        if key == _live_state["last"]:
            continue
        _live_state["last"] = key
        with _live_lock:
            # Save cursor, paint the two rows, restore -- typed input keeps
            # its position because it lives on a different row.
            sys.stdout.write("\033[s" + r8 + "\033[K" + r9 + "\033[K\033[u")
            sys.stdout.flush()


def show_command_table(commands):
    """
    Display available commands in a clean table.
    commands: list of (name, description) tuples.
    """
    max_name = max(len(name) for name, _ in commands)
    for name, desc in commands:
        padding = " " * (max_name - len(name) + 2)
        print(f"    {BOLD}{CYAN}{name}{RESET}{padding}{DIM}{desc}{RESET}")


# ── Xapyra live dashboard + arrow-key picker ────────────────────────────
# Same visual language as above. render() keeps a fixed-height layout contract
# so `status` output is unit-testable without a running engine.
WIDTH = 64
HEIGHT = 10  # render() always emits this many lines

def message(text: str, level: int = 1) -> None:
    """betterVpack-compatible status line. 1 info, 2 success, 3 warning, 4 error."""
    if level == 2:
        print(f"{BOLD}{GREEN}Success:{RESET} {text}")
    elif level == 3:
        print(f"{BOLD}{YELLOW}Warning:{RESET} {text}")
    elif level == 4:
        print(f"{BOLD}{RED}Error:{RESET} {text}")
    else:
        print(text)


def _row(text: str) -> str:
    text = text[: WIDTH - 4]
    return f"| {text.ljust(WIDTH - 4)} |"


def _bar(frac: float, width: int = 20) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = round(frac * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


# -- interactive picker (Gemini-CLI-style menu, betterVpack colours) --------- #
def read_key() -> str:
    """One keypress: 'up' | 'down' | 'enter' | 'esc' | single char. q aborts."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
        if ch == "\r":
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            if select.select([sys.stdin], [], [], 0.1)[0]:
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq, "esc")
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class PickerAbort(Exception):
    """User hit q / esc instead of picking."""


def choose(title: str, options: list[str], default: int = 0) -> int:
    """Arrow-key menu. Number keys jump, Enter confirms, q/esc aborts.

    Falls back to typed numbers when stdin is not a TTY.
    """
    if not options:
        raise ValueError("choose() needs at least one option")
    if not sys.stdin.isatty():
        message(f"{title}", 1)
        for i, opt in enumerate(options):
            print(f"  {i + 1}. {opt}")
        while True:
            try:
                raw = input(f"pick [1-{len(options)}] (default {default + 1}): ").strip()
            except EOFError:
                raise PickerAbort
            if not raw:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            message(f"enter 1-{len(options)}", 4)

    idx = max(0, min(default, len(options) - 1))
    n = len(options)
    message(f"{title}", 1)
    print(f"{DIM}up/down + Enter, or 1-{n}, q quits{RESET}")
    for _ in range(n):
        print("")
    while True:
        sys.stdout.write(f"\033[{n}A")
        for i, opt in enumerate(options):
            mark = f"{BOLD}{GREEN}>{RESET}" if i == idx else " "
            style = f"{BOLD}" if i == idx else ""
            sys.stdout.write(f"\033[K{mark} {style}{i + 1}. {opt}{RESET}\n")
        sys.stdout.flush()
        key = read_key()
        if key == "up":
            idx = (idx - 1) % n
        elif key == "down":
            idx = (idx + 1) % n
        elif key == "enter":
            sys.stdout.write("\n")
            return idx
        elif key in ("q", "esc"):
            raise PickerAbort
        elif key.isdigit() and 1 <= int(key) <= n:
            sys.stdout.write("\n")
            return int(key) - 1


def ask(prompt: str, default: str = "") -> str:
    """betterVpack-style free-text prompt with a default."""
    hint = f" [{default}]" if default else ""
    try:
        raw = input(f"{BOLD}{prompt}{RESET}{hint}: ").strip()
    except EOFError:
        raise PickerAbort
    return raw or default


def render(st: dict) -> str:
    """Fixed-height dashboard box from an `/api/status`-shaped dict."""
    dev = st.get("device", {})
    timing = st.get("timing", {})
    ring = st.get("ring", {})
    rec = st.get("recording", {})

    live = bool(dev.get("connected"))
    dot = f"{GREEN}LIVE{RESET}" if live else f"{RED}DOWN{RESET}"
    head = f"XAPYRA {dev.get('host', '?')}:{dev.get('port', '?')}  [{dot}]"

    total = dev.get("total_bytes", 0) or 0
    obs = timing.get("observed_bytes_per_sec")
    rate = f"{obs:,.0f} B/s" if obs else "-- B/s"
    line2 = f"session #{dev.get('sessions', 0)}  {total:,} bytes  {rate}"

    wall = timing.get("wall_seconds", 0.0) or 0.0
    audio = timing.get("audio_seconds", 0.0) or 0.0
    drift = timing.get("drift_seconds", 0.0) or 0.0
    drift_col = GREEN if abs(drift) < 1.0 else (YELLOW if abs(drift) < 5.0 else RED)
    line3 = f"1:1 wall {wall:.1f}s  audio {audio:.1f}s  drift {drift_col}{drift:+.1f}s{RESET}"
    gap = timing.get("gap_seconds", 0.0) or 0.0
    vsess = timing.get("drift_vs_connected", 0.0) or 0.0
    line3b = f"connected {timing.get('connected_seconds', 0.0) or 0.0:.1f}s  gap {gap:.1f}s  in-session {vsess:+.1f}s"

    retained = ring.get("retention_seconds", 0) or 0
    buffered = ring.get("buffered_seconds", 0) or 0
    frac = (buffered / retained) if retained else 0.0
    line4 = f"ring {_bar(frac)} {buffered:.1f}/{retained:g}s"

    if rec.get("active"):
        rname = rec.get("path") or "?"
        # total_written is the in-memory truth; file_bytes lags behind stdio
        # buffering (64 KB chunks) and bytes_written resets every session.
        rbytes = rec.get("total_written")
        if not rbytes:
            rbytes = rec.get("file_bytes")
        if rbytes is None:
            rbytes = rec.get("bytes_written", 0) or 0
        line5 = f"REC {rname}  {rbytes:,} bytes"
    else:
        line5 = "REC idle (--no-record or link down)"

    err = dev.get("last_error")
    line6 = f"last error: {err}" if err else "last error: none"

    foot = f"{DIM}Ctrl+C quit | http://localhost:{st.get('server', {}).get('port', 8081)}/ | green LED blinks while LIVE{RESET}"

    border = "+" + "-" * (WIDTH - 2) + "+"
    lines = [
        border,
        _row(f"{BOLD}{head}{RESET}"),
        _row(line2),
        _row(line3),
        _row(line3b),
        _row(line4),
        _row(line5),
        _row(line6),
        border,
        foot,
    ]
    return "\n".join(lines)


async def dashboard(link, http_port: int, stop: asyncio.Event, interval: float = 0.5) -> None:
    """Redraw the box in place until `stop` fires. All state is polled, so a
    dead link degrades to DOWN instead of killing the display."""
    first = True
    while not stop.is_set():
        st = {
            "device": {
                "host": link.host,
                "port": link.port,
                "connected": link.connected,
                "sessions": link.sessions,
                "total_bytes": link.total_bytes,
                "last_error": link.last_error,
            },
            "timing": link.timing_stats(),
            "ring": link.ring.stats(),
            "recording": link.recorder.stats()
            if link.record
            else {"active": False, "path": None, "bytes_written": 0, "file_bytes": None},
            "server": {"port": http_port},
        }
        box = render(st)
        if first and sys.stdout.isatty():
            sys.stdout.write("\n" * (HEIGHT - 1))
            first = False
        if sys.stdout.isatty():
            sys.stdout.write(f"\033[{HEIGHT - 1}A\033[J")
        sys.stdout.write(box + "\n")
        sys.stdout.flush()
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
