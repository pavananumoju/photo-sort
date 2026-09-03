"""Glue: scan -> group -> (review) -> apply. Source-agnostic; talks only to PhotoSource."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

# parallel thumbnail downloads during scan (network-bound, so > CPU count is fine)
SCAN_WORKERS = 12

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
    config_path: str, thumb_px: int, refresh: bool, state_dir: Path,
    console: Console | None = None, similarity: int = 12,
    progress_cb: "Callable[[int, int, str], None] | None" = None,
) -> dict:
    """Fingerprint + group every photo. Returns a summary dict.

    `console` (CLI) drives a rich progress bar; `progress_cb` (web UI) is called
    as ``cb(done, total, message)`` after every photo. Either, both, or neither.
    """
    cfg = Config.load(config_path)
    src_list = [{"type": s.type, "roots": list(s.roots)} for s in cfg.sources]
    store = Store(state_dir)
    fp_cache: dict = {} if refresh else store.load_fingerprints()

    scanned: list[ScannedPhoto] = []
    new_fp_cache: dict = {}

    # list every source up front so the total (and ETA) is known from photo #1
    listed: list[tuple] = []
    for spec in cfg.sources:
        source = build_source(spec)
        if console:
            console.print(f"[bold]Source[/] {spec.type} :: {', '.join(spec.roots)}")
        refs = list(source.list_photos(spec.roots))
        if console:
            console.print(f"  {len(refs)} photos")
        listed.append((source, refs))
    total = sum(len(r) for _, r in listed)
    if progress_cb:
        progress_cb(0, total, "listing complete")

    done = 0
    rp = task = None
    if console:
        rp = Progress(console=console)
        rp.start()
        task = rp.add_task("fingerprinting", total=total)

    lock = threading.Lock()

    def tick(msg: str) -> None:
        nonlocal done
        with lock:
            done += 1
            d = done
        if rp:
            rp.advance(task)
        if progress_cb:
            progress_cb(d, total, msg)

    def fingerprint_one(source, ref) -> None:
        """Download + analyse one uncached photo. Runs in a worker thread."""
        key = photo_key(ref.source_id, ref.id)
        cache_tag = f"{ref.size}:{int(ref.created.timestamp()) if ref.created else 0}"
        if isinstance(source, LocalFolderSource) and not ref.checksum_md5:
            ref = _with_md5(ref, LocalFolderSource.md5(ref.id))
        try:
            thumb = source.thumbnail(ref, max_px=thumb_px)
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't kill the run
            if console:
                console.print(f"    [yellow]skip[/] {ref.name}: {e}")
            tick(f"skipped {ref.name}")
            return
        store.write_thumb(key, thumb)
        facts = analyse_thumbnail(thumb, None, None)
        sp = ScannedPhoto(
            ref=ref, phash=facts.phash, dhash=facts.dhash,
            width=facts.width, height=facts.height,
            sharpness=facts.sharpness, brightness=facts.brightness,
            flags=list(facts.flags), thumb_path=str(store.thumb_path_for(key)),
        )
        entry = {
            "tag": cache_tag, "phash": facts.phash, "dhash": facts.dhash,
            "sharpness": facts.sharpness, "brightness": facts.brightness,
            "flags": [f.value for f in facts.flags], "md5": ref.checksum_md5,
            "name": ref.name, "location": ref.location, "size": ref.size,
            "source_id": ref.source_id, "id": ref.id,
            "created": ref.created.isoformat() if ref.created else None,
        }
        with lock:
            scanned.append(sp)
            new_fp_cache[key] = entry
        tick(ref.name)

    try:
        for source, refs in listed:
            todo = []
            for ref in refs:
                key = photo_key(ref.source_id, ref.id)
                cache_tag = f"{ref.size}:{int(ref.created.timestamp()) if ref.created else 0}"
                cached = fp_cache.get(key)
                if cached and cached.get("tag") == cache_tag and not refresh:
                    scanned.append(_from_cache(ref, cached, store))
                    new_fp_cache[key] = cached
                    tick(ref.name)
                else:
                    todo.append(ref)

            # download/analyse the uncached ones in parallel (network-bound)
            if todo:
                with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                    list(pool.map(lambda r: fingerprint_one(source, r), todo))
            source.close()
    finally:
        if rp:
            rp.stop()

    store.save_fingerprints(new_fp_cache)

    groups = group_photos(scanned, phash_threshold=similarity)
    payload = _scan_payload(scanned, groups)
    payload["sources"] = src_list
    store.save_scan(payload)
    # a fresh scan is a fresh grouping with fresh keeper suggestions -- drop any
    # keep/quarantine picks from a previous (or crashed) run so Apply can't act
    # on a stale grouping. Same policy as `regroup`.
    store.save_decisions({})

    dup_count = sum(len(g["members"]) - 1 for g in payload["groups"])
    reclaim = sum(m["size"] for g in payload["groups"] for m in g["members"][1:])
    flagged = [s for s in payload["photos"] if s["flags"]]
    summary = {
        "photos": len(scanned),
        "groups": len(groups),
        "removable": dup_count,
        "reclaim_bytes": reclaim,
        "flagged": len(flagged),
        "sources": src_list,
    }
    if console:
        console.print()
        console.print(
            f"[bold green]{len(groups)}[/] duplicate groups covering "
            f"[bold]{dup_count}[/] removable photos (~{_mb(reclaim)} MB)."
        )
        console.print(f"[bold yellow]{len(flagged)}[/] photos flagged (blurry / dark / blown out).")
        console.print("Next: [bold]photo-sort review[/]")
    return summary


def run_apply(
    state_dir: Path, assume_yes: bool = True, console: Console | None = None,
    config_path: str = "photo-sort.toml",
    progress_cb: "Callable[[int, int, str], None] | None" = None,
) -> dict:
    """Move every photo marked 'quarantine' into its source's review area. Never deletes."""
    store = Store(state_dir)
    scan = store.load_scan()
    decisions = store.load_decisions()
    to_move = [k for k, v in decisions.items() if v == "quarantine"]
    if not to_move:
        if console:
            console.print("Nothing marked for removal.")
        return {"moved": 0, "total": 0, "targets": _review_targets(config_path)}

    if console:
        console.print(f"About to move [bold]{len(to_move)}[/] photos into each source's review area.")
        console.print("Originals are only [i]moved[/], never deleted.")
        if not assume_yes:
            typer_confirm(console)

    by_id = {photo_key(p["source_id"], p["id"]): p for p in scan["photos"]}

    # rebuild one live source object per source_id
    cfg = Config.load(config_path)
    source_objs: dict = {}
    for spec in cfg.sources:
        s = build_source(spec)
        bound = False
        for ref in s.list_photos(spec.roots):
            source_objs[ref.source_id] = s
            bound = True
            break
        if not bound:
            source_objs[spec.type] = s

    total = len(to_move)
    moved = 0
    moved_keys: list[str] = []   # decisions to retire once acted on
    used_sources: dict = {}      # source_id -> live source object that received a move
    review_dirs: set[str] = set()  # local destination folders actually written to
    for i, key in enumerate(to_move, 1):
        p = by_id.get(key)
        if not p:
            if progress_cb:
                progress_cb(i, total, "missing from scan")
            continue
        s = source_objs.get(p["source_id"])
        if not s:
            if console:
                console.print(f"[yellow]no live source for[/] {p['name']}")
            if progress_cb:
                progress_cb(i, total, f"no source for {p['name']}")
            continue
        ref = PhotoRef(
            source_id=p["source_id"], id=p["id"], name=p["name"],
            location=p["location"], size=p["size"], checksum_md5=p.get("md5"),
        )
        where = s.quarantine(ref)
        moved += 1
        moved_keys.append(key)
        used_sources[p["source_id"]] = s
        if where and where.startswith("/"):
            review_dirs.add(str(Path(where).parent))
        if console:
            console.print(f"  moved {p['name']} -> {where}")
        if progress_cb:
            progress_cb(i, total, f"moved {p['name']}")

    if moved_keys:
        # retire acted-on picks so a second Apply doesn't re-move the same files
        store.save_decisions({k: v for k, v in decisions.items() if k not in set(moved_keys)})

    targets: list[dict] = []
    for s in used_sources.values():
        t = s.review_target()
        if t and t not in targets:
            targets.append(t)
    for d in sorted(review_dirs):
        targets.append({"kind": "path", "label": "photo-sort review folder", "value": d})

    if console:
        console.print(
            f"[bold green]Done.[/] {moved} photos moved. Review them, then delete for good yourself."
        )
        for t in targets:
            console.print(f"  {t['label']}: {t['value']}")
    return {"moved": moved, "total": total, "targets": targets}


def _review_targets(config_path: str) -> list[dict]:
    """Best-effort ``review_target()`` for every configured source, for the UI to
    link to even when nothing was moved this run."""
    out: list[dict] = []
    try:
        for spec in Config.load(config_path).sources:
            t = build_source(spec).review_target()
            if t and t not in out:
                out.append(t)
    except Exception:  # noqa: BLE001 - a missing link must not fail apply
        pass
    return out


def regroup(state_dir: Path, similarity: int) -> dict:
    """Re-run only the grouping step at a new strictness, straight from the
    fingerprint cache. No source, no network, no thumbnails -- milliseconds.
    """
    store = Store(state_dir)
    cache = store.load_fingerprints()
    if not cache:
        raise FileNotFoundError("No scan yet — run a scan first.")

    scanned: list[ScannedPhoto] = []
    for key, c in cache.items():
        source_id, _, pid = key.partition("::")
        ref = PhotoRef(
            source_id=c.get("source_id", source_id),
            id=c.get("id", pid),
            name=c.get("name", pid),
            location=c.get("location", ""),
            size=int(c.get("size", 0)),
            created=_parse_iso(c.get("created")),
            checksum_md5=c.get("md5"),
        )
        scanned.append(
            ScannedPhoto(
                ref=ref,
                phash=c.get("phash"),
                dhash=c.get("dhash"),
                sharpness=c.get("sharpness"),
                brightness=c.get("brightness"),
                flags=[_flag(x) for x in c.get("flags", [])],
                thumb_path=str(store.thumb_path_for(key)),
            )
        )

    groups = group_photos(scanned, phash_threshold=similarity)
    payload = _scan_payload(scanned, groups)
    store.save_scan(payload)

    dup_count = sum(len(g["members"]) - 1 for g in payload["groups"])
    reclaim = sum(m["size"] for g in payload["groups"] for m in g["members"][1:])
    flagged = [s for s in payload["photos"] if s["flags"]]
    return {
        "photos": len(scanned),
        "groups": len(groups),
        "removable": dup_count,
        "reclaim_bytes": reclaim,
        "flagged": len(flagged),
    }


def _parse_iso(s: str | None):
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _flag(x: str):
    from photo_sort.model import FlagReason

    return FlagReason(x)


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
