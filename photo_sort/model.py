"""Core data types, shared by every source and every stage of the pipeline.

Nothing here knows about Google Drive, pen drives, or the filesystem. A source
produces `PhotoRef`s; the pipeline groups them into `PhotoGroup`s; the reviewer
turns user choices into a `Decision` list; `apply` acts on those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class PhotoRef:
    """One photo in some source. `id` is opaque and only meaningful to that source."""

    source_id: str            # which configured source this came from, e.g. "gdrive" / "local:/Volumes/PEN"
    id: str                   # source-specific handle (Drive file id, or absolute path)
    name: str                 # file name for display
    location: str             # human-readable folder ("Dandelli Trip" or "/Volumes/PEN/DCIM")
    size: int                 # bytes
    created: datetime | None = None
    checksum_md5: str | None = None   # only if the source provides it for free (Drive does)


class FlagReason(str, Enum):
    BLURRY = "blurry"
    TOO_DARK = "too_dark"
    TOO_BRIGHT = "too_bright"
    SCREENSHOT = "screenshot"


@dataclass
class ScannedPhoto:
    """A PhotoRef plus everything `scan` computed about it."""

    ref: PhotoRef
    phash: str | None = None          # perceptual hash (hex string)
    dhash: str | None = None
    width: int | None = None
    height: int | None = None
    sharpness: float | None = None    # variance-of-Laplacian; lower = blurrier
    brightness: float | None = None   # mean luminance 0-255
    flags: list[FlagReason] = field(default_factory=list)
    thumb_path: str | None = None     # local cached thumbnail


@dataclass
class PhotoGroup:
    """A set of photos the tool believes are the same shot. First member is the suggested keeper."""

    key: str                          # stable id for the group
    members: list[ScannedPhoto]
    kind: str = "near_duplicate"      # "exact" | "near_duplicate" | "burst"

    @property
    def keeper(self) -> ScannedPhoto:
        return self.members[0]

    @property
    def duplicates(self) -> list[ScannedPhoto]:
        return self.members[1:]


class Action(str, Enum):
    KEEP = "keep"
    QUARANTINE = "quarantine"   # move to the review/trash area of its source


@dataclass
class Decision:
    photo_id: str
    source_id: str
    action: Action
