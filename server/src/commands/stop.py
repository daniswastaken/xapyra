"""Stop the capture run and print the 1:1 report."""
from src import ui


def _fmt(report: dict) -> None:
    ui.header("1:1 Report")
    rows = [
        ("file", str(report.get("file", "?"))),
        ("wall", f"{report['wall_seconds']:.1f}s"),
        ("connected", f"{report['connected_seconds']:.1f}s"),
        ("gap (no link)", f"{report['gap_seconds']:.1f}s"),
        ("audio", f"{report['audio_seconds']:.1f}s"),
        ("drift", f"{report['drift_seconds']:+.1f}s"),
        ("in-session drift", f"{report['drift_vs_connected']:+.1f}s"),
        ("observed", f"{report['observed_bytes_per_sec']} B/s"),
    ]
    ui.show_command_table(rows)
    print()
    if report["gap_seconds"] > 1.0 and abs(report["drift_vs_connected"]) < max(
        5.0, 0.02 * report["connected_seconds"]
    ):
        ui.info(f"Drift is all dropout gap ({report['gap_seconds']:.0f}s with no link) -- in-session audio 1:1.")


def run(ctx, args: list[str]) -> None:
    if not ctx.engine.running:
        ui.warn("Nothing recording.")
        return
    report = ctx.engine.stop()
    if report is None:
        ui.warn("Stopped before any audio arrived -- nothing to report.")
        return
    _fmt(report)
