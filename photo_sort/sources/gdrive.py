"""Google Drive source.

Auth is a one-time browser login. You need a desktop OAuth client from Google
Cloud (free); drop the downloaded file next to the project as ``client_secret.json``.
See docs/google-drive-setup.md. The login token is cached in ``token.json`` and
reused after that.

Scope: full ``drive`` -- the tool needs write access to *move* existing photos
into a review folder. It never deletes; a quarantined photo is one folder move
away from where it was, and Drive keeps it recoverable for 30 days if you then
empty it into Trash yourself.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from photo_sort.model import PhotoRef
from photo_sort.sources.base import PhotoSource

SCOPES = ["https://www.googleapis.com/auth/drive"]
REVIEW_FOLDER_NAME = "photo-sort review"
_PAGE_FIELDS = (
    "nextPageToken, files(id, name, mimeType, size, md5Checksum, createdTime, "
    "imageMediaMetadata(width,height), thumbnailLink, parents)"
)


class GoogleDriveSource(PhotoSource):
    source_id = "gdrive"

    def __init__(self, client_secret: str = "client_secret.json", token: str = "token.json") -> None:
        self._client_secret = Path(client_secret)
        self._token = Path(token)
        self._svc = None
        self._review_folder_id: str | None = None
        self._authed_session = None

    # ---- auth / service -------------------------------------------------
    def _service(self):
        if self._svc is not None:
            return self._svc
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if self._token.exists():
            creds = Credentials.from_authorized_user_file(str(self._token), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self._client_secret.exists():
                    raise RuntimeError(
                        f"Missing {self._client_secret}. Create a free 'Desktop app' OAuth client at "
                        "console.cloud.google.com > APIs & Services > Credentials, download it here, "
                        "and rerun. Full steps: docs/google-drive-setup.md"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(self._client_secret), SCOPES)
                creds = flow.run_local_server(port=0)
            self._token.write_text(creds.to_json())
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._svc

    # ---- listing ------------------------------------------------------
    def _resolve_folder_id(self, name_or_id: str) -> str:
        svc = self._service()
        # treat as an id first
        try:
            meta = svc.files().get(fileId=name_or_id, fields="id, mimeType").execute()
            if meta.get("mimeType") == "application/vnd.google-apps.folder":
                return meta["id"]
        except Exception:
            pass
        q = (
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
            f"and name = {_q(name_or_id)}"
        )
        res = svc.files().list(q=q, fields="files(id, name)", pageSize=10).execute()
        folders = res.get("files", [])
        if not folders:
            raise FileNotFoundError(f"No Drive folder named {name_or_id!r}")
        if len(folders) > 1:
            raise RuntimeError(f"{len(folders)} Drive folders named {name_or_id!r}; pass its folder id instead")
        return folders[0]["id"]

    def list_photos(self, roots: list[str]) -> Iterator[PhotoRef]:
        svc = self._service()
        for root in roots:
            root_id = self._resolve_folder_id(root)
            yield from self._walk(svc, root_id, root)

    def _walk(self, svc, folder_id: str, folder_label: str) -> Iterator[PhotoRef]:
        stack: list[tuple[str, str]] = [(folder_id, folder_label)]
        while stack:
            fid, label = stack.pop()
            page = None
            while True:
                res = (
                    svc.files()
                    .list(
                        q=f"'{fid}' in parents and trashed = false",
                        fields=_PAGE_FIELDS,
                        pageSize=1000,
                        pageToken=page,
                    )
                    .execute()
                )
                for f in res.get("files", []):
                    mt = f.get("mimeType", "")
                    if mt == "application/vnd.google-apps.folder":
                        stack.append((f["id"], f"{label}/{f['name']}"))
                    elif mt.startswith("image/"):
                        meta = f.get("imageMediaMetadata") or {}
                        yield PhotoRef(
                            source_id=self.source_id,
                            id=f["id"],
                            name=f["name"],
                            location=label,
                            size=int(f.get("size", 0)),
                            created=_parse_ts(f.get("createdTime")),
                            checksum_md5=f.get("md5Checksum"),
                        )
                page = res.get("nextPageToken")
                if not page:
                    break

    # ---- bytes ------------------------------------------------------
    def _session(self):
        if self._authed_session is None:
            from google.auth.transport.requests import AuthorizedSession

            self._authed_session = AuthorizedSession(self._service()._http.credentials)
        return self._authed_session

    def thumbnail(self, ref: PhotoRef, max_px: int = 256) -> bytes:
        svc = self._service()
        meta = svc.files().get(fileId=ref.id, fields="thumbnailLink").execute()
        link = meta.get("thumbnailLink")
        if link:
            # thumbnailLink ends with '=s220'; swap the size we want
            link = link.rsplit("=s", 1)[0] + f"=s{max_px}"
            r = self._session().get(link, timeout=30)
            if r.ok and r.content:
                return r.content
        # fallback: download original, resize locally
        from PIL import Image

        with Image.open(io.BytesIO(self.full_bytes(ref))) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            return buf.getvalue()

    def full_bytes(self, ref: PhotoRef) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        svc = self._service()
        req = svc.files().get_media(fileId=ref.id)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    # ---- quarantine ------------------------------------------------------
    def _review_folder(self) -> str:
        if self._review_folder_id:
            return self._review_folder_id
        svc = self._service()
        q = (
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
            f"and name = {_q(REVIEW_FOLDER_NAME)} and 'root' in parents"
        )
        res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
        if res.get("files"):
            self._review_folder_id = res["files"][0]["id"]
        else:
            made = svc.files().create(
                body={"name": REVIEW_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
                fields="id",
            ).execute()
            self._review_folder_id = made["id"]
        return self._review_folder_id

    def quarantine(self, ref: PhotoRef) -> str:
        svc = self._service()
        dest = self._review_folder()
        cur = svc.files().get(fileId=ref.id, fields="parents").execute()
        prev = ",".join(cur.get("parents", []))
        svc.files().update(
            fileId=ref.id, addParents=dest, removeParents=prev, fields="id, parents"
        ).execute()
        return f"Drive/{REVIEW_FOLDER_NAME}/{ref.name}"


def _q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
