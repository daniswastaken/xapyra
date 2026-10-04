"""Shared constants, clock helper and logger namespace for the audio engine."""

from __future__ import annotations

import logging
import time
from datetime import datetime

LOG = logging.getLogger("xapyra.retrieval")

MAX_HEADER_BYTES = 16 * 1024
IO_CHUNK = 64 * 1024
CRLF = b"\r\n"
SUBSCRIBER_QUEUE = 512
IDLE_SESSION_TIMEOUT = 20.0
IDLE_STREAM_TIMEOUT = 10.0

REASONS = {
    200: "OK",
    206: "Partial Content",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    416: "Range Not Satisfiable",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def iso(ts: float | None) -> str | None:
    """Local-time ISO stamp, or None."""
    return None if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
