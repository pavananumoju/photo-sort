"""Turn per-photo fingerprints into groups of "same shot".

Two photos are linked when their perceptual hashes are within a small Hamming
distance, OR they share an exact md5. Links are transitive, so we use union-find
to collapse chains (A~B, B~C => one group of A,B,C).
"""

from __future__ import annotations

import imagehash

from photo_sort.model import PhotoGroup, ScannedPhoto

# Max bits that may differ between two 64-bit perceptual hashes to still count as
# the same shot.
#
# What this reliably catches at ~12: the same picture saved twice, resized,
# re-compressed, or WhatsApp-forwarded (the 12 MB vs 1.3 MB pairs). That is the
# bulk of a cluttered Drive.
#
# What it does NOT reliably catch: burst shots where people moved between frames
# (measured 16-32 bits apart in testing, overlapping the range of genuinely
# unrelated photos). Those need embedding-based similarity -- see roadmap.
#
# Raising this finds more loose matches but risks merging unrelated photos into
# one group; every group is confirmed in review, so a moderate value is safe.
DEFAULT_PHASH_THRESHOLD = 12


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


def group_photos(
    photos: list[ScannedPhoto],
    phash_threshold: int = DEFAULT_PHASH_THRESHOLD,
) -> list[PhotoGroup]:
    n = len(photos)
    uf = _UnionFind(n)

    # 1. exact matches by md5 (free from Drive; computed for local)
    by_md5: dict[str, int] = {}
    for i, p in enumerate(photos):
        m = p.ref.checksum_md5
        if m:
            if m in by_md5:
                uf.union(by_md5[m], i)
            else:
                by_md5[m] = i

    # 2. near matches by perceptual hash. O(n^2) is fine into the low thousands;
    #    swap for a BK-tree / bucketing if a source ever gets much bigger.
    hashes = [(_hex_to_hash(p.phash) if p.phash else None) for p in photos]
    for i in range(n):
        hi = hashes[i]
        if hi is None:
            continue
        for j in range(i + 1, n):
            hj = hashes[j]
            if hj is None:
                continue
            if (hi - hj) <= phash_threshold:
                uf.union(i, j)

    # collect
    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(uf.find(i), []).append(i)

    groups: list[PhotoGroup] = []
    for root, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        members = [photos[i] for i in idxs]
        members.sort(key=_keeper_rank)  # best first
        exact = len({m.ref.checksum_md5 for m in members if m.ref.checksum_md5}) == 1 and all(
            m.ref.checksum_md5 for m in members
        )
        groups.append(
            PhotoGroup(
                key=f"g{root}",
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
