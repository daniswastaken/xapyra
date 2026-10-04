"""Saved session recordings."""
from pathlib import Path

from src import ui
from src.audio.recorder import list_recordings


def run(ctx, args: list[str]) -> None:
    outdir = Path(ctx.outdir)
    recs = list_recordings(outdir)
    if not recs:
        ui.info(f"No recordings in {outdir}/ yet.")
        return
    ui.header("Recordings")
    ui.show_command_table([
        (r["name"], f"{r['bytes']:,} bytes  {r['modified']}")
        for r in recs
    ])
    print()
