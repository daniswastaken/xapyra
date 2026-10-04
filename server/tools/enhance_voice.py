#!/usr/bin/env python3
"""Voice-focused post-processing for Xapyra captures.

Mirrors the firmware DSP (HP 120Hz -> LP 5-6kHz -> presence -> FFT denoise
-> compressor -> loudnorm limiter) using ffmpeg filters, so existing
recordings can be cleaned without re-recording. Needs only ffmpeg on PATH.

Usage:
  python server/tools/enhance_voice.py server/recordings/xapyra-....mp3
  python server/tools/enhance_voice.py --all
  python server/tools/enhance_voice.py in.mp3 -o out_voice.mp3 [--strong]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_IN = REPO / "server" / "recordings"


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def build_filter(strong: bool) -> str:
    nr = "afftdn=nr=25:nf=-30" if not strong else "afftdn=nr=30:nf=-25"
    # HP 120 / LP 4.5k x2 (24dB/oct, poles max 2 per stage) / presence +8dB
    # @3k / FFT denoise / gentle compressor / loudnorm to -16 LUFS speech
    parts = [
        "highpass=f=120:poles=2",
        "lowpass=f=4500:poles=2",
        "lowpass=f=4500:poles=2",
        "equalizer=f=3000:t=q:w=1:g=8",
        nr,
        "acompressor=threshold=-20dB:ratio=4:attack=10:release=200:makeup=8dB",
        "loudnorm=I=-16:TP=-2:LRA=11",
    ]
    return ",".join(parts)


def enhance(src: Path, dst: Path, strong: bool) -> None:
    f = build_filter(strong)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-ac", "1", "-ar", "32000",
        "-af", f, "-b:a", "128k", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {src.name}:\n{r.stderr[-2000:]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Clean Xapyra captures for voice.")
    ap.add_argument("input", nargs="?", help="input mp3 (default: --all)")
    ap.add_argument("-o", "--output", help="output mp3")
    ap.add_argument("--all", action="store_true", help="process all recordings/")
    ap.add_argument("--strong", action="store_true", help="stronger denoise")
    ap.add_argument("--outdir", default="server/enhanced",
                    help="output dir for --all (default: server/enhanced)")
    a = ap.parse_args(argv)

    if not have_ffmpeg():
        print("ffmpeg not found on PATH.", file=sys.stderr)
        return 1

    targets: list[Path] = []
    if a.all:
        targets = sorted(DEFAULT_IN.glob("*.mp3"))
        if not targets:
            print(f"no mp3 in {DEFAULT_IN}", file=sys.stderr)
            return 1
    elif a.input:
        targets = [Path(a.input)]
    else:
        ap.print_help()
        return 2

    outdir = REPO / a.outdir
    for src in targets:
        if not src.is_file():
            print(f"skip missing {src}", file=sys.stderr)
            continue
        if a.output and len(targets) == 1:
            dst = Path(a.output)
        else:
            outdir.mkdir(parents=True, exist_ok=True)
            dst = outdir / f"{src.stem}_voice.mp3"
        enhance(src, dst, a.strong)
        print(f"ok: {src.name} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
