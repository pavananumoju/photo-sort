"""Turn per-photo fingerprints into groups of "same shot".

Grouping is two-stage:

1. Perceptual hash (phash) is a cheap, coarse filter: two photos within
   `phash_threshold` Hamming bits are *candidates* for the same shot. Photos
   sharing an exact md5 are always the same shot (byte-identical), no further
   check needed.
2. Every candidate pair is then confirmed with a windowed SSIM comparison of
   the actual thumbnails (see `_ssim_similar`). phash is a coarse structural
   hash of the whole frame -- when the background/framing/lighting hold still
   (a tripod burst, a posed photo session) it barely moves even though a
   person's *pose* changed completely, so phash alone over-merges those into
   one group. SSIM looks at local pixel structure and catches that: a real
   duplicate (resave/recompress/forward) still scores ~0.9+, a different pose
   in the same scene drops well below that.

Candidates are joined "star" style around a representative photo (the
chronologically-earliest one in each cluster) rather than chained pairwise
(A~B~C~D...). Pairwise-chained (single-linkage) clustering lets a slow burst
of gradually-changing poses drag the whole sequence into one group even though
the first and last frames look nothing alike, because each *adjacent* pair
happens to pass the check. Comparing every candidate back to one fixed
representative avoids that drift.
"""

from __future__ import annotations

import imagehash
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

from photo_sort.model import PhotoGroup, ScannedPhoto

# Max bits that may differ between two 64-bit perceptual hashes to still count as
# a *candidate* same-shot pair (confirmed or rejected by SSIM below).
#
# Raising this finds more loose candidates but costs more SSIM comparisons;
# since SSIM does the real filtering now, this mostly just controls how wide a
# net gets cast before the precise check.
DEFAULT_PHASH_THRESHOLD = 12

# Minimum windowed SSIM (0-1, on grayscale 160x160 thumbnails) for a
# phash-candidate pair to be confirmed as the same shot.
#
# Calibrated against a real photo set: a genuine duplicate (same pose, ~1s
# apart) scored ~0.86; a different pose in the same posed-photo session
# (same background/lighting) scored ~0.42-0.69. 0.82 sits between those,
# closer to the duplicate side.
DEFAULT_SSIM_MIN = 0.82

_SSIM_SIZE = 160


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _hex_to_hash(h: str):
    return imagehash.hex_to_hash(h)


def _load_gray(thumb_path: str):
    """Grayscale, fixed-size array for SSIM."""
    try:
        with Image.open(thumb_path) as im:
            im = im.convert("L").resize((_SSIM_SIZE, _SSIM_SIZE), Image.BILINEAR)
            return np.asarray(im, dtype=np.float64)
    except OSError:
        return None


def _ssim_similar(a: ScannedPhoto, b: ScannedPhoto, ssim_min: float, gray_cache: dict) -> bool:
    """Confirm a phash-candidate pair is really the same shot.

    If either thumbnail can't be loaded, we have no way to confirm -- fail
    closed (not similar) rather than risk merging unrelated photos.

    `gray_cache` is scoped to one `group_photos` call: a representative photo
    gets compared against many candidates within a run, but thumbnails on
    disk can be overwritten by a later rescan, so nothing here outlives the call.
    """
    if not a.thumb_path or not b.thumb_path:
        return False
    for p in (a.thumb_path, b.thumb_path):
        if p not in gray_cache:
            gray_cache[p] = _load_gray(p)
    ga, gb = gray_cache[a.thumb_path], gray_cache[b.thumb_path]
    if ga is None or gb is None:
        return False
    return ssim(ga, gb, data_range=255) >= ssim_min


def _created_key(p: ScannedPhoto):
    return p.ref.created or _far_future()


def group_photos(
    photos: list[ScannedPhoto],
    phash_threshold: int = DEFAULT_PHASH_THRESHOLD,
    ssim_min: float = DEFAULT_SSIM_MIN,
) -> list[PhotoGroup]:
    n = len(photos)
    uf = _UnionFind(n)

    # 1. exact matches by md5 (free from Drive; computed for local). Byte-identical,
    #    so no SSIM check needed -- they're the same shot by definition.
    by_md5: dict[str, int] = {}
    for i, p in enumerate(photos):
        m = p.ref.checksum_md5
        if m:
            if m in by_md5:
                uf.union(by_md5[m], i)
            else:
                by_md5[m] = i

    # md5 pre-clusters become single units: near-dup matching below picks one
    # representative per unit rather than treating each photo independently.
    unit_of_root: dict[int, list[int]] = {}
    for i in range(n):
        unit_of_root.setdefault(uf.find(i), []).append(i)
    units = list(unit_of_root.values())
    # earliest photo in each unit stands in for the whole unit
    for u in units:
        u.sort(key=lambda i: _created_key(photos[i]))
    units.sort(key=lambda u: _created_key(photos[u[0]]))

    # 2. near matches: phash for candidates, SSIM to confirm, joined "star"
    #    style around a representative to avoid single-linkage chain drift
    #    (see module docstring).
    hashes = [(_hex_to_hash(p.phash) if p.phash else None) for p in photos]
    cluster_reps: list[int] = []          # photo index representing each cluster
    cluster_units: list[list[list[int]]] = []  # units assigned to each cluster
    gray_cache: dict = {}

    for unit in units:
        rep_i = unit[0]
        hi = hashes[rep_i]
        placed = False
        if hi is not None:
            for ci, rep in enumerate(cluster_reps):
                hr = hashes[rep]
                if hr is None or (hi - hr) > phash_threshold:
                    continue
                if _ssim_similar(photos[rep_i], photos[rep], ssim_min, gray_cache):
                    cluster_units[ci].append(unit)
                    placed = True
                    break
        if not placed:
            cluster_reps.append(rep_i)
            cluster_units.append([unit])

    groups: list[PhotoGroup] = []
    for gi, unit_list in enumerate(cluster_units):
        idxs = [i for unit in unit_list for i in unit]
        if len(idxs) < 2:
            continue
        members = [photos[i] for i in idxs]
        members.sort(key=_keeper_rank)  # best first
        exact = len({m.ref.checksum_md5 for m in members if m.ref.checksum_md5}) == 1 and all(
            m.ref.checksum_md5 for m in members
        )
        groups.append(
            PhotoGroup(
                key=f"g{gi}",
                members=members,
                kind="exact" if exact else "near_duplicate",
            )
        )
    # biggest groups first — most to gain
    groups.sort(key=lambda g: len(g.members), reverse=True)
    return groups


def _keeper_rank(p: ScannedPhoto) -> tuple:
    """Lower sorts first = the one we suggest keeping.

    Prefer: more pixels, then sharper, then larger file, then earliest capture.
    """
    px = (p.width or 0) * (p.height or 0)
    return (-px, -(p.sharpness or 0.0), -p.ref.size, p.ref.created or _far_future())


def _far_future():
    from datetime import datetime, timezone

    return datetime(9999, 1, 1, tzinfo=timezone.utc)
