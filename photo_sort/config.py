"""Load ``photo-sort.toml`` and turn each ``[[source]]`` block into a live source."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from photo_sort.sources.base import PhotoSource
from photo_sort.sources.gdrive import GoogleDriveSource
from photo_sort.sources.local import LocalFolderSource

DEFAULT_CONFIG_NAME = "photo-sort.toml"

EXAMPLE = """\
# Which places to scan for photos. Add as many [[source]] blocks as you like.

[[source]]
type  = "gdrive"
roots = ["Dandelli Trip"]          # Drive folder names (or folder ids)

# [[source]]
# type  = "local"                  # a pen drive, external HDD, or any folder
# roots = ["/Volumes/MyPenDrive/DCIM"]
"""


@dataclass
class SourceSpec:
    type: str
    roots: list[str]


@dataclass
class Config:
    sources: list[SourceSpec]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_NAME) -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"No {p} found. Create one — starter contents:\n\n{EXAMPLE}"
            )
        data = tomllib.loads(p.read_text())
        specs = [
            SourceSpec(type=s["type"], roots=list(s["roots"]))
            for s in data.get("source", [])
        ]
        if not specs:
            raise ValueError(f"{p} has no [[source]] blocks.")
        return cls(sources=specs)


def build_source(spec: SourceSpec) -> PhotoSource:
    if spec.type == "gdrive":
        return GoogleDriveSource()
    if spec.type == "local":
        return LocalFolderSource()
    raise ValueError(f"Unknown source type {spec.type!r} (known: gdrive, local)")
