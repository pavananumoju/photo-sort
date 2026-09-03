"""Command-line entry point.

    photo-sort scan     # look at every configured source, group duplicates, flag bad shots
    photo-sort review   # open a local web page to pick what to remove
    photo-sort apply    # act on those picks (move to each source's review area)
    photo-sort status   # what the last scan found

State for a run lives in ./.photo-sort/ (thumbnails + a small SQLite index).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()

STATE_DIR = Path(".photo-sort")


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
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """Open the local review page for the most recent scan."""
    from photo_sort.review_server import serve

    serve(state_dir=STATE_DIR, host=host, port=port, console=console)


@app.command()
def apply(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Move every photo marked 'remove' in review into its source's review area. Never deletes."""
    from photo_sort.pipeline import run_apply

    run_apply(state_dir=STATE_DIR, assume_yes=yes, console=console)


@app.command()
def status() -> None:
    """Summarise the last scan: groups found, space recoverable, flags."""
    from photo_sort.pipeline import print_status

    print_status(state_dir=STATE_DIR, console=console)


if __name__ == "__main__":
    app()
