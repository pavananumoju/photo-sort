"""Glue: scan -> group -> (review) -> apply. Source-agnostic; talks only to PhotoSource."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from photo_sort.config import Config, build_source
from photo_sort.grouping import group_photos
from photo_sort.imaging import analyse_thumbnail
from photo_sort.model import PhotoRef, ScannedPhoto
from photo_sort.sources.local import LocalFolderSource
from photo_sort.store import Store, photo_key

FLAG_LABELS = {
    "blurry": "blurry",
    "too_dark": "very dark",
    "too_bright": "blown out",
    "screenshot": "screenshot",
}


def run_scan(
    config_path: str, thumb_px: int, refresh: bool, state_dir: Path, console: Console,
    similarity: int = 12,
) -> None:
    cfg = Config.load(config_path)
    store = Store(state_dir)
    fp_cache: dict = {} if refresh else store.load_fingerprints()

    scanned: list[ScannedPhoto] = []
    new_fp_cache: dict = {}

    for spec in cfg.sources:
        source = build_source(spec)
        console.print(f"[bold]Source[/] {spec.type} :: {', '.join(spec.roots)}")
        refs = list(source.list_photos(spec.roots))
        console.print(f"  {len(refs)} photos")

        with Progress(console=console) as progress:
            task = progress.add_task("  fingerprinting", total=len(refs))
            for ref in refs:
                key = photo_key(ref.source_id, ref.id)
                cache_tag = f"{ref.size}:{int(ref.created.timestamp()) if ref.created else 0}"

                cached = fp_cache.get(key)
                if cached and cached.get("tag") == cache_tag and not refresh:
                    scanned.append(_from_cache(ref, cached, store))
                    new_fp_cache[key] = cached
                    progress.advance(task)
                    continue

                # local files: fill md5 now so exact-dup detection works
                if isinstance(source, LocalFolderSource) and not ref.checksum_md5:
                    ref = _with_md5(ref, LocalFolderSource.md5(ref.id))

                try:
                    thumb = source.thumbnail(ref, max_px=thumb_px)
                except Exception as e:  # noqa: BLE001 - one bad file shouldn't kill the run
                    console.print(f"    [yellow]skip[/] {ref.name}: {e}")
                    progress.advance(task)
                    continue

                store.write_thumb(key, thumb)
                facts = analyse_thumbnail(thumb, ref.width if hasattr(ref, "width") else None, None)
                sp = ScannedPhoto(
                    ref=ref,
                    phash=facts.phash,
                    dhash=facts.dhash,
                    width=facts.width,
                    height=facts.height,
                    sharpness=facts.sharpness,
                    brightness=facts.brightness,
                    flags=list(facts.flags),
                    thumb_path=str(store.thumb_path_for(key)),
                )
                scanned.append(sp)
                new_fp_cache[key] = {
                    "tag": cache_tag,
                    "phash": facts.phash,
                    "dhash": facts.dhash,
                    "sharpness": facts.sharpness,
                    "brightness": facts.brightness,
                    "flags": [f.value for f in facts.flags],
                    "md5": ref.checksum_md5,
                    "name": ref.name,
                    "location": ref.location,
                    "size": ref.size,
                    "source_id": ref.source_id,
                    "id": ref.id,
                    "created": ref.created.isoformat() if ref.created else None,
                }
                progress.advance(task)
        source.close()

    store.save_fingerprints(new_fp_cache)

    groups = group_photos(scanned, phash_threshold=similarity)
    payload = _scan_payload(scanned, groups)
    store.save_scan(payload)

    dup_count = sum(len(g["members"]) - 1 for g in payload["groups"])
    reclaim = sum(
        m["size"] for g in payload["groups"] for m in g["members"][1:]
    )
    flagged = [s for s in payload["photos"] if s["flags"]]
    console.print()
    console.print(
        f"[bold green]{len(groups)}[/] duplicate groups covering "
        f"[bold]{dup_count}[/] removable photos (~{_mb(reclaim)} MB)."
    )
    console.print(f"[bold yellow]{len(flagged)}[/] photos flagged (blurry / dark / blown out).")
    console.print("Next: [bold]photo-sort review[/]")


def run_apply(state_dir: Path, assume_yes: bool, console: Console) -> None:
    store = Store(state_dir)
    scan = store.load_scan()
    decisions = store.load_decisions()
    if not decisions:
        console.print("No decisions found. Run [bold]photo-sort review[/] and mark photos first.")
        raise SystemExit(1)

    to_move = [k for k, v in decisions.items() if v == "quarantine"]
    if not to_move:
        console.print("Nothing marked for removal.")
        return

    console.print(f"About to move [bold]{len(to_move)}[/] photos into each source's review area.")
    console.print("Originals are only [i]moved[/], never deleted.")
    if not assume_yes:
        typer_confirm(console)

    by_source = {}
    for p in scan["photos"]:
        by_source.setdefault(p["source_id"], {})[photo_key(p["source_id"], p["id"])] = p

    # rebuild the right source object per source_id
    cfg = Config.load("photo-sort.toml")
    source_objs = {}
    for spec in cfg.sources:
        s = build_source(spec)
        # list once to bind source_id(s); cheap compared to the move itself
        for ref in s.list_photos(spec.roots):
            source_objs[ref.source_id] = s
            break
        else:
            source_objs[spec.type] = s

    moved = 0
    for key in to_move:
        p = next((pp for pp in scan["photos"] if photo_key(pp["source_id"], pp["id"]) == key), None)
        if not p:
            continue
        s = source_objs.get(p["source_id"])
        if not s:
            console.print(f"[yellow]no live source for[/] {p['name']}")
            continue
        ref = PhotoRef(
            source_id=p["source_id"], id=p["id"], name=p["name"],
            location=p["location"], size=p["size"], checksum_md5=p.get("md5"),
        )
        where = s.quarantine(ref)
        moved += 1
        console.print(f"  moved {p['name']} -> {where}")
    console.print(f"[bold green]Done.[/] {moved} photos moved. Review them, then delete for good yourself.")


def print_status(state_dir: Path, console: Console) -> None:
    store = Store(state_dir)
    try:
        scan = store.load_scan()
    except FileNotFoundError as e:
        console.print(str(e))
        return
    t = Table("group", "kind", "keeper", "duplicates", "reclaim MB")
    for g in scan["groups"][:50]:
        members = g["members"]
        t.add_row(
            g["key"],
            g["kind"],
            members[0]["name"],
            str(len(members) - 1),
            _mb(sum(m["size"] for m in members[1:])),
        )
    console.print(t)
    console.print(f"{len(scan['groups'])} groups total, {len(scan['photos'])} photos scanned.")


# ---- helpers ----
def _scan_payload(scanned: list[ScannedPhoto], groups) -> dict:
    def sp_json(sp: ScannedPhoto) -> dict:
        return {
            "source_id": sp.ref.source_id,
            "id": sp.ref.id,
            "name": sp.ref.name,
            "location": sp.ref.location,
            "size": sp.ref.size,
            "created": sp.ref.created.isoformat() if sp.ref.created else None,
            "md5": sp.ref.checksum_md5,
            "phash": sp.phash,
            "width": sp.width,
            "height": sp.height,
            "sharpness": sp.sharpness,
            "brightness": sp.brightness,
            "flags": [f.value for f in sp.flags],
            "thumb": sp.thumb_path,
        }

    return {
        "photos": [sp_json(s) for s in scanned],
        "groups": [
            {"key": g.key, "kind": g.kind, "members": [sp_json(m) for m in g.members]}
            for g in groups
        ],
    }


def _from_cache(ref: PhotoRef, c: dict, store: Store) -> ScannedPhoto:
    from photo_sort.model import FlagReason

    key = photo_key(ref.source_id, ref.id)
    return ScannedPhoto(
        ref=_with_md5(ref, c.get("md5")) if c.get("md5") and not ref.checksum_md5 else ref,
        phash=c.get("phash"),
        dhash=c.get("dhash"),
        sharpness=c.get("sharpness"),
        brightness=c.get("brightness"),
        flags=[FlagReason(x) for x in c.get("flags", [])],
        thumb_path=str(store.thumb_path_for(key)),
    )


def _with_md5(ref: PhotoRef, md5: str | None) -> PhotoRef:
    from dataclasses import replace

    return replace(ref, checksum_md5=md5)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f}"


def typer_confirm(console: Console) -> None:
    import typer

    if not typer.confirm("Proceed?"):
        console.print("Cancelled.")
        raise SystemExit(0)
