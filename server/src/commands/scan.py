"""Find ESP32 responders: parallel TCP sweep for the :8080 stream port."""
import asyncio

from src import ui
from src.net.discover import DEFAULT_PORT, discover, local_ipv4s


def run(ctx, args: list[str]) -> None:
    port = DEFAULT_PORT
    subnets: list[str] | None = None
    for a in args:
        if a.startswith(":"):
            port = int(a[1:])
        elif "/" in a:
            subnets = (subnets or []) + [a]
    scope = f"subnet(s) {', '.join(subnets)}" if subnets else (
        f"local /24(s) around {', '.join(local_ipv4s()) or 'no NIC found'}"
    )
    ui.step(f"Scanning {scope} for :{port} ...")
    found = asyncio.run(discover(port, subnets=subnets))
    ctx.last_scan = found
    if not found:
        ui.error(f"Nothing answering on :{port}. Is the ESP32 on this network?")
        return
    ui.success(f"{len(found)} device(s) on :{port}")
    ui.show_command_table([(ip, "ESP32 stream port open") for ip in found])
    print()
