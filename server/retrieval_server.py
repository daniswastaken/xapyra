#!/usr/bin/env python3
"""Backwards-compatible flag entry: `python server/retrieval_server.py [flags]`.

Canonical code now lives in src/: audio engine in src/audio, flag parsing in
src/cli, discovery in src/net, UI in src/ui. This module only re-exports the
public names the tests and scripts import, and forwards __main__ to the CLI.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.audio import (
    IO_CHUNK,
    REASONS,
    AudioRing,
    DeviceLink,
    HttpError,
    Recorder,
    Reply,
    Request,
    RetrievalService,
    iso,
    list_recordings,
)
from src.cli import build_parser, configure_logging, env, main, safe_output_name

__all__ = [
    "AudioRing",
    "DeviceLink",
    "Recorder",
    "RetrievalService",
    "Request",
    "Reply",
    "HttpError",
    "list_recordings",
    "iso",
    "IO_CHUNK",
    "REASONS",
    "build_parser",
    "configure_logging",
    "env",
    "main",
    "safe_output_name",
]

if __name__ == "__main__":
    sys.exit(main())
