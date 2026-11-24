"""Shared download / store steps used by every source."""
import hashlib
import re
import sqlite3
import uuid
from pathlib import Path

from PIL import Image

from . import storage
from .client import PoliteClient
from .config import Settings

# Still images and animated images only; video/audio/documents are out of scope.
MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/webp": "webp",
    "image/tiff": "tif",
}
EXT_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
            "svg": "image/svg+xml", "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff"}


def mime_from_name(name: str) -> str | None:
    return EXT_MIME.get(name.rsplit(".", 1)[-1].lower()) if "." in name else None


PIL_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp", "TIFF": "image/tiff"}


def sniff_mime(path: Path) -> str | None:
    """Image type from the content, for URLs without an extension."""
    head = path.read_bytes()[:512]
    if b"<svg" in head.lower():
        return "image/svg+xml"
    try:
        with Image.open(path) as im:
            return PIL_MIME.get(im.format)
    except Exception:
        return None


class Fetched:
    """A file downloaded to the temp dir, with its checksums."""

    def __init__(self, path: Path, size: int, sha1: str, sha256: str):
        self.path, self.size, self.sha1, self.sha256 = path, size, sha1, sha256


def fetch(s: Settings, client: PoliteClient, url: str, max_bytes: int | None = None) -> Fetched:
    s.tmp_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_bytes or s.max_file_bytes
    tmp = s.tmp_dir / f"{uuid.uuid4().hex}.part"
    sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
    try:
        with client.get(url, stream=True) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"file larger than {max_bytes // 2**20} MB")
                    sha1.update(chunk)
                    sha256.update(chunk)
                    f.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Fetched(tmp, size, sha1.hexdigest(), sha256.hexdigest())


def from_bytes(s: Settings, data: bytes) -> Fetched:
    """Wrap in-memory content (e.g. a zip member) as a Fetched temp file."""
    s.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = s.tmp_dir / f"{uuid.uuid4().hex}.part"
    tmp.write_bytes(data)
    return Fetched(tmp, len(data), hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest())


def store_image(s: Settings, con: sqlite3.Connection, f: Fetched, mime: str,
                width: int | None = None, height: int | None = None) -> str:
    """Store a fetched file once per content hash and record it in `images`. Returns sha256.

    The temp file is always consumed. width/height are used for SVG, which Pillow cannot open.
    """
    try:
        if con.execute("SELECT 1 FROM images WHERE sha256 = ?", (f.sha256,)).fetchone():
            return f.sha256
        frames = 1
        if mime != "image/svg+xml":
            Image.MAX_IMAGE_PIXELS = None      # large scans are legitimate; size is already capped
            with Image.open(f.path) as im:
                width, height, frames = im.width, im.height, getattr(im, "n_frames", 1)
        ext = MIME_EXT[mime]
        disk, mirror, rel = storage.store(s, f.path, f.sha256, ext)
        con.execute("INSERT INTO images (sha256, ext, mime, bytes, width, height, frames, disk, "
                    "mirror_disk, rel_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f.sha256, ext, mime, f.size, width, height, frames, disk, mirror, rel))
        return f.sha256
    finally:
        f.path.unlink(missing_ok=True)


def mark_stored(con: sqlite3.Connection, item_id: int, sha256: str) -> None:
    con.execute("UPDATE items SET status = 'stored', image_sha256 = ?, error = NULL, "
                "stored_at = datetime('now') WHERE id = ?", (sha256, item_id))


# Network failures worth retrying; permanent ones (checksum mismatch, unreadable image, too large) stay errors.
TRANSIENT_ERROR = re.compile(r"Connection|Read timed out|IncompleteRead|Max retries|\b5\d\d Server Error")


def retry_transient_errors(con: sqlite3.Connection, source: str, retry_status: str = "meta_ok") -> int:
    """Put items that failed for network reasons back in the queue (called at the start of each run)."""
    rows = con.execute("SELECT id, error FROM items WHERE source = ? AND status = 'error'", (source,)).fetchall()
    ids = [r["id"] for r in rows if r["error"] and TRANSIENT_ERROR.search(r["error"])]
    con.executemany("UPDATE items SET status = ?, error = NULL WHERE id = ?", [(retry_status, i) for i in ids])
    con.commit()
    return len(ids)


def mark_error(con: sqlite3.Connection, item_id: int, error: Exception | str) -> None:
    con.execute("UPDATE items SET status = 'error', error = ? WHERE id = ?", (str(error)[:500], item_id))
