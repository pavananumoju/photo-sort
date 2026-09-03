"""Load ``photo-sort.toml`` and turn each ``[[source]]`` block into a live source."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from photo_sort.sources.base import PhotoSource
from photo_sort.sources.gdrive import GoogleDriveSource
from photo_sort.sources.local import LocalFolderSource

DEFAULT_CONFIG_NAME = "photo-sort.toml"

KNOWN_TYPES = ("gdrive", "local")

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

    @classmethod
    def from_pairs(cls, pairs: list[dict]) -> "Config":
        """Build from the web UI's flat ``[{type, root}, ...]`` list.

        Roots are merged into one ``[[source]]`` block per type, order preserved,
        duplicates dropped.
        """
        order: list[str] = []
        roots_by_type: dict[str, list[str]] = {}
        for pair in pairs:
            t = str(pair.get("type", "")).strip()
            r = str(pair.get("root", "")).strip()
            if t not in KNOWN_TYPES:
                raise ValueError(f"Unknown source type {t!r} (known: {', '.join(KNOWN_TYPES)})")
            if not r:
                continue
            if t not in roots_by_type:
                roots_by_type[t] = []
                order.append(t)
            if r not in roots_by_type[t]:
                roots_by_type[t].append(r)
        return cls(sources=[SourceSpec(type=t, roots=roots_by_type[t]) for t in order])

    def dumps(self) -> str:
        out = ["# photo-sort sources. Managed by the photo-sort web UI (Sources card).", ""]
        for spec in self.sources:
            roots = ", ".join(_toml_str(r) for r in spec.roots)
            out += ["[[source]]", f'type  = "{spec.type}"', f"roots = [{roots}]", ""]
        return "\n".join(out)

    def save(self, path: str | Path = DEFAULT_CONFIG_NAME) -> None:
        Path(path).write_text(self.dumps())


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_source(spec: SourceSpec) -> PhotoSource:
    if spec.type == "gdrive":
        return GoogleDriveSource()
    if spec.type == "local":
        return LocalFolderSource()
    raise ValueError(f"Unknown source type {spec.type!r} (known: gdrive, local)")
