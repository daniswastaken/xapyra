"""Session recordings on disk -- never truncate prior audio."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from src.audio._common import IO_CHUNK, iso

LOG = logging.getLogger("xapyra.recorder")

# --------------------------------------------------------------------------- #
# disk recording
# --------------------------------------------------------------------------- #
class Recorder:
    """Writes the stream to disk.

    Without `filename` each device session gets its own timestamped file. With
    `filename` (--file) every session appends to one fixed file: opened
    truncating when the server starts, reopened in append mode on each
    reconnect so the file is one continuous capture rather than one file per
    dropout.
    """

    def __init__(self, outdir: Path, filename: str | None = None) -> None:
        self._outdir = outdir
        self._filename = filename
        self._fh = None
        self._opened_once = False
        self.path: Path | None = None
        self.started_at: float | None = None
        self.bytes_written = 0
        self.total_written = 0

    def start(self) -> None:
        if self._fh is not None:
            return
        if self._filename is not None:
            self.path = self._outdir / self._filename
            if not self._opened_once and self.path.is_file() and self.path.stat().st_size > 0:
                # Server restart with --file: the old capture is real audio.
                # Move it aside instead of truncating it into oblivion.
                backup = self.path.with_name(
                    f"{self.path.stem}-{datetime.now():%Y%m%d-%H%M%S}.bak{self.path.suffix}"
                )
                self.path.rename(backup)
                LOG.info("previous capture preserved as %s", backup)
            # First open of this server run starts clean; reconnects append.
            mode = "ab" if self._opened_once else "wb"
        else:
            # Timestamps only have 1s resolution; a fast drop+reconnect can
            # land in the same second and truncate the previous session file.
            # Never reuse a name that already exists on disk.
            base = f"xapyra-{datetime.now():%Y%m%d-%H%M%S}"
            self.path = self._outdir / f"{base}.mp3"
            n = 2
            while self.path.exists():
                self.path = self._outdir / f"{base}-{n}.mp3"
                n += 1
            mode = "wb"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self._fh = self.path.open(mode, buffering=IO_CHUNK)
        self._opened_once = True
        self.bytes_written = 0
        LOG.info("recording -> %s", self.path)

    def write(self, data: bytes) -> None:
        if self._fh is None:
            return
        self._fh.write(data)
        self.bytes_written += len(data)
        self.total_written += len(data)

    def stop(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        LOG.info("recording saved: %s (%d bytes)", self.path, self.bytes_written)
        self._fh = None

    def stats(self) -> dict:
        file_bytes = None
        if self.path is not None and self.path.is_file():
            with contextlib.suppress(OSError):
                file_bytes = self.path.stat().st_size
        return {
            "active": self._fh is not None,
            "path": self.path.name if self.path else None,
            "started_at": iso(self.started_at),
            "bytes_written": self.bytes_written,
            "total_written": self.total_written,
            "file_bytes": file_bytes,
        }


def list_recordings(outdir: Path) -> list[dict]:
    if not outdir.is_dir():
        return []
    out = []
    for p in sorted(outdir.glob("*.mp3"), key=lambda q: q.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "bytes": st.st_size, "modified": iso(st.st_mtime)})
    return out


# --------------------------------------------------------------------------- #
