"""Visiome Platform crawler (visiome.neuroinf.jp).

The site publishes every item's metadata as one JSON file (items.json). We take the
Stimulus items, minus non-illusion collections, and for each one store:
  - every stimulus_data image file;
  - for picture/other items without such an image: the images inside stimulus_data zips;
  - if there is still nothing: the preview image.
Each item carries its own license in `rights`, which we normalize to a short name.
"""
import io
import json
import logging
import re
import sqlite3
import zipfile
from datetime import date

from . import ingest, storage
from .client import PoliteClient
from .config import Settings

log = logging.getLogger(__name__)

SOURCE = "visiome"
HOST = "visiome.neuroinf.jp"
BASE = f"https://{HOST}/modules/xoonips"
ITEMS_JSON = f"{BASE}/items.json"
EXCLUDED_OWNERS = {"PrimFace Database Committee"}   # monkey/chimp face photo database, not illusions
ZIP_TYPES = {"picture", "other"}                      # zips of movie/program items hold videos and code
# Basic psychophysics stimulus sets (CC BY, Hosokawa & Maruya; Ohzawa): kept, but flagged as not illusions.
NOT_ILLUSION_ITEMS = {
    6799: "sinusoidal grating set",
    6801: "random noise set",
    6802: "random dot pattern set",
    6803: "Gaussian window set",
    3181: "contrast sensitivity (CSF) chart images",
}
MAX_ZIP_BYTES = 300 * 1024 * 1024   # Kitaoka's ECVP2012 exhibition zip is 169 MB


def make_client(s: Settings) -> PoliteClient:
    return PoliteClient(s.user_agent, {HOST: s.download_interval})


# ---------------------------------------------------------------- license

CC_URL = "https://creativecommons.org/licenses/{}/{}/"


def normalize_license(rights: str | None) -> tuple[str, str | None]:
    """'Creative Commons Attribution-NonCommercial 4.0 ...' -> ('CC BY-NC 4.0', url)."""
    text = (rights or "").strip()
    low = text.lower()
    if "creative commons" in low:
        parts = ["by"]
        if "noncommercial" in low or "non-commercial" in low:
            parts.append("nc")
        if "noderiv" in low or "no deriv" in low:
            parts.append("nd")
        elif "sharealike" in low or "share alike" in low:
            parts.append("sa")
        version = (re.search(r"\b(\d\.\d)\b", text) or [None, "4.0"])[1]
        code = "-".join(parts)
        return f"CC {code.upper()} {version}", CC_URL.format(code, version)
    if not text:
        return "unknown", None
    return "All rights reserved", None    # full wording is kept in raw_json


# ---------------------------------------------------------------- 1. list

def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _pub_date(x: dict) -> str | None:
    y, m, d = x.get("publication_year"), x.get("publication_month"), x.get("publication_mday")
    if not y:
        return None
    if m and d:
        return date(y, m, d).isoformat()
    return f"{y}-{m:02d}" if m else str(y)


def _clean(text: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (text or "").replace("\r", "\n")).strip()
    return text or None


def list_items(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    """Fetch items.json and register the files to collect (idempotent)."""
    resp = client.get(ITEMS_JSON)
    resp.raise_for_status()
    s.work_dir.joinpath("cache").mkdir(exist_ok=True)
    s.work_dir.joinpath("cache", "visiome-items.json").write_bytes(resp.content)
    items = resp.json()

    stimuli = [x for x in items if x["item_type_name"] == "xnpstimulus" and x["name"] not in EXCLUDED_OWNERS]
    n_new = 0
    for x in stimuli:
        files = x.get("file", [])
        data = [f for f in files if f["file_type_name"] == "stimulus_data"]
        picks = [(f, "file") for f in data if ingest.mime_from_name(f["original_file_name"])]
        if not picks and x.get("stimulus_type") in ZIP_TYPES:
            picks = [(f, "zip") for f in data if _ext(f["original_file_name"]) == "zip"]
        if not picks:
            picks = [(f, "preview") for f in files
                     if f["file_type_name"] == "preview" and ingest.mime_from_name(f["original_file_name"])]
        for f, kind in picks:
            n_new += _register(con, x, f, kind)
    con.commit()
    log.info("visiome: %d stimulus items, %d new files registered", len(stimuli), n_new)
    flag_non_illusions(con)


def flag_non_illusions(con: sqlite3.Connection) -> None:
    n = 0
    for item_id, reason in NOT_ILLUSION_ITEMS.items():
        n += con.execute("INSERT OR IGNORE INTO item_flags (item_id, flag, reason) "
                         "SELECT id, 'not_illusion', ? FROM items WHERE source = ? "
                         "AND (source_id LIKE ? OR source_id = ?)",
                         (reason, SOURCE, f"{item_id}/%", str(item_id))).rowcount
    con.commit()
    if n:
        log.info("visiome: flagged %d files as not_illusion", n)


def _register(con: sqlite3.Connection, x: dict, f: dict, kind: str) -> int:
    source_id = f"{x['item_id']}/{f['file_id']}"
    license_, license_url = normalize_license(x.get("rights"))
    description = "\n\n".join(t for t in (_clean(x.get("description")), _clean(x.get("readme"))) if t) or None
    raw = {"item": x, "file": f, "kind": kind}
    cur = con.execute(
        """INSERT OR IGNORE INTO items (source, source_id, page_url, file_url, mime, bytes, title, description,
               author, date_created, license, license_url, attribution_required, raw_json, status, meta_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (SOURCE, source_id,
         f"{BASE}/detail.php?item_id={x['item_id']}",
         f"{BASE}/file/{f['file_id']}/{f['original_file_name']}",
         "application/zip" if kind == "zip" else ingest.mime_from_name(f["original_file_name"]),
         f.get("file_size"), x.get("title"), description,
         ", ".join(x.get("developer") or []) or x.get("name"),
         _pub_date(x), license_, license_url,
         1 if license_.startswith("CC") else None,
         json.dumps(raw, ensure_ascii=False),
         "zip_pending" if kind == "zip" else "meta_ok"))
    if cur.rowcount:
        item_id = cur.lastrowid
        cats = [i["title"] for i in x.get("index", [])] + [f"keyword:{k}" for k in x.get("keyword", [])]
        cats.append(f"stimulus_type:{x.get('stimulus_type')}")
        con.executemany("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                        [(item_id, c) for c in cats])
    return cur.rowcount


# ---------------------------------------------------------------- 2. download

def download(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    if ingest.retry_transient_errors(con, SOURCE):
        # zips go back to zip_pending; zip members are only stored by re-expanding their zip
        con.execute("UPDATE items SET status = 'zip_pending' WHERE source = ? AND status = 'meta_ok' "
                    "AND mime = 'application/zip'", (SOURCE,))
        con.execute("UPDATE items SET status = 'zip_member' WHERE source = ? AND status = 'meta_ok' "
                    "AND json_extract(raw_json, '$.zip_member') IS NOT NULL", (SOURCE,))
        con.commit()
    rows = con.execute("SELECT * FROM items WHERE source = ? AND status IN ('meta_ok', 'zip_pending') "
                       "ORDER BY id", (SOURCE,)).fetchall()
    for row in rows:
        try:
            if row["status"] == "zip_pending":
                _expand_zip(s, con, client, row)
            else:
                f = ingest.fetch(s, client, row["file_url"])
                ingest.mark_stored(con, row["id"], ingest.store_image(s, con, f, row["mime"]))
        except storage.StorageError:
            raise
        except Exception as e:
            log.warning("failed %s: %s", row["source_id"], e)
            ingest.mark_error(con, row["id"], e)
        con.commit()
    log.info("visiome download finished: %d entries", len(rows))


def _expand_zip(s: Settings, con: sqlite3.Connection, client: PoliteClient, row: sqlite3.Row) -> None:
    """Download a zip and register + store each image inside it as its own item."""
    f = ingest.fetch(s, client, row["file_url"], max_bytes=MAX_ZIP_BYTES)
    try:
        data = f.path.read_bytes()
    finally:
        f.path.unlink(missing_ok=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            mime = ingest.mime_from_name(name)
            if info.is_dir() or not mime or "__MACOSX/" in name or name.split("/")[-1].startswith("._"):
                continue
            source_id = f"{row['source_id']}/{name}"
            raw = json.loads(row["raw_json"]) | {"zip_member": name}
            # status 'zip_member' (not 'meta_ok') so download() never fetches the zip URL as an image;
            # a member is only ever stored from here, and an interrupted zip is redone on the next run.
            cur = con.execute(
                """INSERT OR IGNORE INTO items (source, source_id, page_url, file_url, mime, bytes, title,
                       description, author, date_created, license, license_url, attribution_required,
                       raw_json, status, meta_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'zip_member', datetime('now'))""",
                (SOURCE, source_id, row["page_url"], row["file_url"], mime, info.file_size,
                 f"{row['title']} ({name.split('/')[-1]})", row["description"], row["author"],
                 row["date_created"], row["license"], row["license_url"], row["attribution_required"],
                 json.dumps(raw, ensure_ascii=False)))
            member = con.execute("SELECT id, status FROM items WHERE source = ? AND source_id = ?",
                                 (SOURCE, source_id)).fetchone()
            member_id = member["id"]
            if cur.rowcount:
                con.execute("INSERT INTO item_categories (item_id, category) "
                            "SELECT ?, category FROM item_categories WHERE item_id = ?", (member_id, row["id"]))
            elif member["status"] == "stored":
                continue
            try:
                fetched = ingest.from_bytes(s, z.read(info))
                ingest.mark_stored(con, member_id, ingest.store_image(s, con, fetched, mime))
                n += 1
            except storage.StorageError:
                raise
            except Exception as e:
                ingest.mark_error(con, member_id, e)
            con.commit()                      # keep write locks short; other crawlers share the DB
    con.execute("UPDATE items SET status = 'expanded', error = NULL, stored_at = datetime('now') "
                "WHERE id = ?", (row["id"],))
    log.info("zip %s: %d images", row["source_id"], n)
