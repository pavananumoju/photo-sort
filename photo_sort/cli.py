"""Command-line entry point.

    photo-sort ui       # everything in one local web page: scan, review, apply (recommended)

    photo-sort scan     # terminal: group duplicates, flag bad shots
    photo-sort review   # terminal-launched web page for the last scan
    photo-sort apply    # terminal: move ticked photos to each source's review area
    photo-sort status   # terminal: summarise the last scan

Run state lives in ./.photo-sort/ (thumbnail cache + small JSON index).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()

STATE_DIR = Path(".photo-sort")


@app.command()
def ui(
    config: str = typer.Option("photo-sort.toml", "--config", "-c"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    no_open: bool = typer.Option(False, "--no-open", help="don't auto-open the browser"),
) -> None:
    """Open the all-in-one local web page (scan with live progress, review, apply)."""
    from photo_sort.webapp import serve

    serve(
        state_dir=STATE_DIR, host=host, port=port, config_path=config,
        console=console, open_browser=not no_open,
    )


@app.command()
def scan(
    config: str = typer.Option("photo-sort.toml", "--config", "-c"),
    thumb_px: int = typer.Option(256, help="thumbnail size used for fingerprinting"),
    similarity: int = typer.Option(
        12, "--similarity", "-s",
        help="how alike photos must be to group (bits; lower = stricter, ~8 = copies only, ~16 = loose)",
    ),
    refresh: bool = typer.Option(False, help="ignore cached fingerprints and redo everything"),
) -> None:
    """Fingerprint every photo, group duplicates, flag blurry/dark/screenshot shots."""
    from photo_sort.pipeline import run_scan

    run_scan(
        config_path=config, thumb_px=thumb_px, similarity=similarity,
        refresh=refresh, state_dir=STATE_DIR, console=console,
    )


@app.command()
def review(
    config: str = typer.Option("photo-sort.toml", "--config", "-c"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """Open the local review/scan page (same page as `ui`)."""
    from photo_sort.webapp import serve

    serve(state_dir=STATE_DIR, host=host, port=port, config_path=config, console=console)


@app.command()
def apply(
    config: str = typer.Option("photo-sort.toml", "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Move every photo marked 'remove' in review into its source's review area. Never deletes."""
    from photo_sort.pipeline import run_apply

    run_apply(state_dir=STATE_DIR, assume_yes=yes, console=console, config_path=config)


@app.command()
def status() -> None:
    """Summarise the last scan: groups found, space recoverable, flags."""
    from photo_sort.pipeline import print_status

    print_status(state_dir=STATE_DIR, console=console)


if __name__ == "__main__":
    app()
