"""The seam that makes photo-sort work the same against Google Drive, a pen
drive, an external HDD, or a plain local folder.

Add a new input type == write one new class that implements `PhotoSource`.
The rest of the tool never changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from photo_sort.model import PhotoRef


class PhotoSource(ABC):
    """Everything the pipeline needs from a place that holds photos."""

    #: short, stable identifier for this configured source, e.g. "gdrive" or "local:/Volumes/PEN"
    source_id: str

    @abstractmethod
    def list_photos(self, roots: list[str]) -> Iterator[PhotoRef]:
        """Yield every image under each root (folder name for Drive, path for local). Recursive."""

    @abstractmethod
    def thumbnail(self, ref: PhotoRef, max_px: int = 256) -> bytes:
        """Return small JPEG bytes for `ref`. Cheap: downloads/reads as little as possible."""

    @abstractmethod
    def full_bytes(self, ref: PhotoRef) -> bytes:
        """Return the original file's bytes. Only called on explicit user request in review."""

    @abstractmethod
    def quarantine(self, ref: PhotoRef) -> str:
        """Move `ref` out of the way into this source's review area.

        Drive: move into a 'photo-sort review' folder (recoverable from Drive Trash for 30 days).
        Local/pen drive: move into '<root>/_photo-sort-review/'.
        Never deletes. Returns a human-readable description of where it went.
        """

    def close(self) -> None:  # optional cleanup hook
        pass
