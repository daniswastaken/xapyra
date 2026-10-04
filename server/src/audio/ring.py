"""Rolling in-memory window of recent MP3 bytes."""

from __future__ import annotations

import time
from collections import deque

# --------------------------------------------------------------------------- #
# audio retention
# --------------------------------------------------------------------------- #
class AudioRing:
    """Rolling window of the last `retention` seconds of MP3 bytes.

    Chunks are kept whole and stamped on arrival, so a window cut lands on a
    chunk boundary -- retrieval is accurate to within one read (a few ms).
    """

    def __init__(self, retention: float, bitrate_kbps: int) -> None:
        self.retention = retention
        self.bytes_per_sec = bitrate_kbps * 1000 // 8
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._size = 0
        self.total_bytes = 0

    def append(self, data: bytes) -> None:
        now = time.monotonic()
        self._chunks.append((now, data))
        self._size += len(data)
        self.total_bytes += len(data)
        cutoff = now - self.retention
        while self._chunks and self._chunks[0][0] < cutoff:
            self._size -= len(self._chunks.popleft()[1])

    def window(self, seconds: float) -> bytes:
        now = time.monotonic()
        cutoff = now - min(seconds, self.retention)
        return b"".join(data for stamp, data in self._chunks if stamp >= cutoff)

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0

    def stats(self) -> dict:
        return {
            "retention_seconds": round(self.retention, 3),
            "buffered_bytes": self._size,
            "buffered_seconds": round(self._size / self.bytes_per_sec, 3) if self.bytes_per_sec else None,
            "total_bytes": self.total_bytes,
        }
