"""Wall vs audio vs gap, with a plain-language verdict."""
from src import ui


def run(ctx, args: list[str]) -> None:
    if not ctx.engine.running:
        ui.warn("Nothing recording -- timings need a live engine.")
        return
    t = ctx.engine.status()["timing"]
    if t.get("first_byte_at") is None:
        ui.info("Engine up, no audio yet -- waiting for the first byte.")
        return
    ui.header("Timings (1:1)")
    ui.show_command_table([
        ("first byte", str(t["first_byte_at"])),
        ("wall", f"{t['wall_seconds']:.1f}s"),
        ("connected", f"{t['connected_seconds']:.1f}s"),
        ("gap (no link)", f"{t['gap_seconds']:.1f}s"),
        ("audio", f"{t['audio_seconds']:.1f}s"),
        ("drift", f"{t['drift_seconds']:+.1f}s"),
        ("in-session drift", f"{t['drift_vs_connected']:+.1f}s"),
        ("observed", f"{t['observed_bytes_per_sec']} B/s (expect {t['expected_bytes_per_sec']})"),
    ])
    print()
    if abs(t["drift_vs_connected"]) < max(5.0, 0.02 * t["connected_seconds"]):
        ui.success("In-session audio is 1:1.")
    else:
        ui.warn("In-session drift is large -- frames are vanishing somewhere.")
