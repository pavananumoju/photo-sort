"""Local folder source. Covers folders on the Mac, plugged-in pen drives, and
external hard drives alike -- on macOS a removable drive is just a folder under
``/Volumes/<name>/``.
"""

from __future__ import annotations

import hashlib
import io
import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from photo_sort.model import PhotoRef
from photo_sort.sources.base import PhotoSource

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
REVIEW_DIRNAME = "_photo-sort-review"


class LocalFolderSource(PhotoSource):
    def __init__(self, root_label: str | None = None) -> None:
        # source_id is finalised per-root during listing; keep a base label for config display
        self.source_id = f"local:{root_label}" if root_label else "local"

    def list_photos(self, roots: list[str]) -> Iterator[PhotoRef]:
        for root in roots:
            base = Path(root).expanduser().resolve()
            if not base.is_dir():
                raise FileNotFoundError(f"Not a folder (is the drive plugged in?): {base}")
            self.source_id = f"local:{base}"
            for path in base.rglob("*"):
                if REVIEW_DIRNAME in path.parts:
                    continue
                if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
                    continue
                st = path.stat()
                yield PhotoRef(
                    source_id=self.source_id,
                    id=str(path),
                    name=path.name,
                    location=str(path.parent),
                    size=st.st_size,
                    created=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    checksum_md5=None,  # computed lazily by the pipeline for exact-dup detection
                )

    def thumbnail(self, ref: PhotoRef, max_px: int = 256) -> bytes:
        with Image.open(ref.id) as im:
            im.draft("RGB", (max_px, max_px))  # fast approximate downscale on decode
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            return buf.getvalue()

    def full_bytes(self, ref: PhotoRef) -> bytes:
        return Path(ref.id).read_bytes()

    def quarantine(self, ref: PhotoRef) -> str:
        src = Path(ref.id)
        # put the review dir at the mount/drive root when we can identify it, else next to the file
        if src.parts[:2] == ("/", "Volumes") and len(src.parts) > 2:
            drive_root = Path("/Volumes") / src.parts[2]  # macOS: a mounted volume
        elif src.drive:
            drive_root = Path(src.anchor)  # Windows: the drive letter, e.g. "D:\\"
        else:
            drive_root = src.parent
        dest_dir = drive_root / REVIEW_DIRNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        i = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}__{i}{src.suffix}"
            i += 1
        shutil.move(str(src), str(dest))
        return str(dest)

    @staticmethod
    def md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
