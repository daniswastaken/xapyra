#!/usr/bin/env python3
"""Find the Xapyra ESP32 on the local network.

The device runs a TCP server on port 8080 (see src/main.cpp), so discovery is
a parallel TCP connect sweep over the local /24 subnet(s). No broadcast, no
mDNS, no dependencies.

Note: a probe briefly occupies the device's single client slot (it accepts,
then sees us hang up). The green LED may flicker once during a scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket

LOG = logging.getLogger("xapyra.discover")

DEFAULT_PORT = 8080
PROBE_TIMEOUT = 0.4
CONCURRENCY = 128


def local_ipv4s() -> list[str]:
    """Non-loopback IPv4 addresses of this machine (e.g. the hotspot NIC)."""
    out: list[str] = []
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    return out


def hosts_in(subnet: str) -> list[str]:
    """All usable host addresses in a subnet. Only /24 or smaller (scan cost)."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        raise ValueError(f"bad subnet {subnet!r}: {exc}") from None
    if net.prefixlen < 24:
        raise ValueError(f"subnet {subnet!r} is larger than /24 -- refusing to scan")
    return [str(h) for h in net.hosts()]


def default_hosts() -> list[str]:
    """Every host on each local /24, minus our own addresses."""
    mine = set(local_ipv4s())
    found: list[str] = []
    for ip in mine:
        try:
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        except ValueError:
            continue
        for h in net.hosts():
            s = str(h)
            if s not in mine and s not in found:
                found.append(s)
    return found


async def probe(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    return True


async def discover(
    port: int = DEFAULT_PORT,
    *,
    hosts: list[str] | None = None,
    subnets: list[str] | None = None,
    timeout: float = PROBE_TIMEOUT,
    concurrency: int = CONCURRENCY,
) -> list[str]:
    """Return sorted IPs with `port` open. Explicit `hosts` win, else `subnets`,
    else every host on the local /24(s)."""
    if hosts is None:
        if subnets:
            seen: set[str] = set()
            targets = []
            for s in subnets:
                for h in hosts_in(s):
                    if h not in seen:
                        seen.add(h)
                        targets.append(h)
        else:
            targets = default_hosts()
    else:
        targets = list(hosts)
    if not targets:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def check(host: str) -> str | None:
        async with sem:
            return host if await probe(host, port, timeout) else None

    found = [h for h in await asyncio.gather(*(check(h) for h in targets)) if h]
    return sorted(found, key=lambda h: tuple(int(p) for p in h.split(".")))


async def discover_one(port: int = DEFAULT_PORT, **kwargs) -> str | None:
    """First responder, or None. Logs what the scan covered."""
    if "hosts" in kwargs and kwargs["hosts"] is not None:
        scope = f"{len(kwargs['hosts'])} explicit host(s)"
    elif "subnets" in kwargs and kwargs["subnets"]:
        scope = f"subnet(s) {', '.join(kwargs['subnets'])}"
    else:
        scope = f"local /24(s) around {', '.join(local_ipv4s()) or 'no NIC found'}"
    LOG.info("scanning %s for port %d ...", scope, port)
    found = await discover(port, **kwargs)
    if not found:
        return None
    if len(found) > 1:
        LOG.warning("multiple responders on port %d: %s -- using %s", port, ", ".join(found), found[0])
    return found[0]
