"""Live state box: link, 1:1 clock, ring fill, recording."""
from src import ui


def run(ctx, args: list[str]) -> None:
    if not ctx.engine.running:
        ui.info("Engine idle -- 'record' to start capturing.")
        return
    st = ctx.engine.status()
    print()
    print(ui.render(st))
    print()
