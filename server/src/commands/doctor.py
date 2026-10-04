"""Environment check: python, HTTP port, and device reachability."""
import shutil
import socket
import sys

from src import ui


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run(ctx, args: list[str]) -> None:
    ui.header("System Status")
    if shutil.which("python") or shutil.which("python3"):
        ui.success(f"Python -- {sys.version.split()[0]}")
    else:
        ui.error("Python -- not found!")
    if _port_free(ctx.http_port):
        ui.success(f"HTTP :{ctx.http_port} -- free")
    else:
        ui.warn(f"HTTP :{ctx.http_port} -- in use (engine running here, or another server)")
    host = args[0] if args else (ctx.last_scan[0] if ctx.last_scan else None)
    if host is None:
        ui.info("Device -- no host given (run 'scan', or: doctor <ip>)")
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect((host, 8080))
        ui.success(f"Device {host}:8080 -- answering")
    except OSError as e:
        ui.error(f"Device {host}:8080 -- unreachable ({e})")
    finally:
        s.close()
    print()
