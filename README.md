# photo-sort

Find duplicate and low-quality photos across your photo sources, review them on a
local page, and move the unwanted ones into a review area. **Nothing is ever
permanently deleted by the tool.**

- **Sources today:** Google Drive.
- **Sources designed for, added later:** pen drives, external hard drives, local
  folders (all handled by one "local folder" source — on macOS a plugged-in drive
  is just `/Volumes/<name>/`).

Runs entirely on your Mac. Only tiny thumbnails are downloaded; originals are
touched only when you approve a move.

## How it works

| Stage | Command | What happens |
|---|---|---|
| Scan | `photo-sort scan` | list photos → download thumbnails → fingerprint (perceptual hash) → group duplicates → flag blurry/dark/blown-out shots |
| Review | `photo-sort review` | opens `localhost:8000`; each group shows keeper + duplicates, tick what to remove |
| Apply | `photo-sort apply` | moves ticked photos into each source's review area (Drive: a `photo-sort review` folder; local: `_photo-sort-review/`) |
| Status | `photo-sort status` | summary of the last scan |

Run state lives in `./.photo-sort/` (thumbnail cache + small JSON index). Safe to
delete. A second `scan` reuses fingerprints and only looks at new/changed photos.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Google Drive needs a one-time login — see [docs/google-drive-setup.md](docs/google-drive-setup.md).

Then create `photo-sort.toml`:

```toml
[[source]]
type  = "gdrive"
roots = ["Dandelli Trip"]        # Drive folder names, or folder ids

# later, when the local source lands:
# [[source]]
# type  = "local"
# roots = ["/Volumes/MyPenDrive/DCIM", "~/Pictures"]
```

## Status

Early build. Google Drive source + scan/group/review/apply wired. Local-folder
source is written behind the same interface but not yet exposed in config.
Roadmap: screenshot detection, burst-vs-duplicate distinction, "keep best of
burst" auto-pick.
