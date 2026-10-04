#!/usr/bin/env python3
"""Post-process Xapyra captures: clean up hiss around and between speech.

WHAT THIS DOES AND DOES NOT DO -- read before trusting a number.

Does: clean the noise floor in the gaps between words.
    Measured on server/recordings/afterfix.mp3 (ffmpeg astats noise floor,
    which is measured in genuinely quiet frames):
        as-is .................. -51.3 dB
        afftdn nr=45 nf=-35 .... -58.5 dB
        afftdn x2 .............. -74.7 dB   <- what this tool applies

Does NOT: remove hiss you hear *while* someone is talking.
    Measured with a stricter metric (4-16kHz hiss relative to the 300-3400Hz
    voice band, over speech frames only), the same file scores -14.6 dB before
    and -13.6 dB after -- i.e. unchanged, within run-to-run variation. That
    hiss is analogue preamp noise from the MAX4466 at maximum gain. It shares
    time-frequency cells with the voice, so no filter separates it; the fix is
    the preamp gain setting, not more DSP. See adcpeak in the device serial log.

Usage:
    python server/tools/enhance_voice.py FILE.mp3 [-o OUT.mp3]
    python server/tools/enhance_voice.py --all
    python server/tools/enhance_voice.py FILE.mp3 --strong
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_IN = REPO / "server" / "recordings"


def build_filter(strong: bool) -> str:
    """Voice band + spectral subtraction.

    lowpass 3.4k x2 : keep the intelligibility band (300-3400Hz carries
        almost all speech intelligibility) and drop everything above it, where
        the preamp hiss lives. Cheap, and the only filter that measurably
        moved the HF floor in testing (-40.5 -> -42.6 dB per 2 poles).
    afftdn x2 : the actual white-noise killer. Pass 1 removes broadband
        hiss, pass 2 cleans what pass 1 left behind. One pass alone gets
        ~7dB; two passes get ~23dB. A single pass leaves audible residue.
    """
    nr, nf = (60, -35) if strong else (45, -35)
    return ",".join([
        "highpass=f=120:poles=2",
        "lowpass=f=3400:poles=2",
        "lowpass=f=3400:poles=2",
        "equalizer=f=2500:t=q:w=1:g=4",
        f"afftdn=nr={nr}:nf={nf}:tn=1",
        f"afftdn=nr={nr}:nf={nf}:tn=1",
        "alimiter=limit=0.95:attack=5:release=50",
    ])


def enhance(src: Path, dst: Path, strong: bool) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-ac", "1", "-ar", "32000",
        "-af", build_filter(strong), "-b:a", "128k", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {src.name}:\n{r.stderr[-2000:]}")


def noise_floor(path: Path) -> str:
    """astats noise floor (quiet-frame level), printed so the result is
    verifiable rather than vibes. This is the gap-between-words number, NOT
    the hiss-during-speech number -- see the module docstring."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-i", str(path),
        "-af", "astats=measure_overall=Noise_floor:measure_perchannel=0",
        "-f", "null", "NUL",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Noise floor dB:" in line:
            return line.split("Noise floor dB:")[1].strip()
    return "?"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Isolate voice in Xapyra captures.")
    ap.add_argument("input", nargs="?", help="input mp3 (use --all for the whole dir)")
    ap.add_argument("-o", "--output", help="output mp3")
    ap.add_argument("--all", action="store_true", help="process every mp3 in server/recordings")
    ap.add_argument("--strong", action="store_true", help="stronger spectral reduction")
    ap.add_argument("--outdir", default="server/enhanced", help="output dir for --all")
    a = ap.parse_args(argv)

    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH.", file=sys.stderr)
        return 1

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
        before = noise_floor(src)
        enhance(src, dst, a.strong)
        after = noise_floor(dst)
        print(f"ok: {src.name} -> {dst}")
        print(f"    noise floor {before} -> {after} dBFS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
