"""Wikimedia Commons crawler (via the MediaWiki API only, no HTML scraping).

Steps, each resumable (state lives in the DB):
  1. categories  — walk the category tree from the seeds, list files
  2. metadata    — fetch imageinfo + extmetadata (license, author, description) in batches of 50
  3. download    — fetch originals, verify Commons SHA-1, store on the HDD pair
"""
import json
import logging
import re
import sqlite3
import time
from html import unescape
from pathlib import Path

from . import ingest, storage
from .client import PoliteClient
from .config import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)

SOURCE = "commons"
API = "https://commons.wikimedia.org/w/api.php"
SEEDS_FILE = PROJECT_ROOT / "sources" / "commons_seeds.txt"
EXCLUDE_FILE = PROJECT_ROOT / "sources" / "commons_exclude.txt"

MIME_EXT = ingest.MIME_EXT


def make_client(s: Settings) -> PoliteClient:
    return PoliteClient(s.user_agent, {
        "commons.wikimedia.org": s.api_interval,
        "upload.wikimedia.org": s.download_interval,
    })


def _read_list(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]


def api(client: PoliteClient, _url: str = API, **params) -> dict:
    """MediaWiki API call (Commons by default; any wiki via _url), honouring maxlag."""
    params = {"action": "query", "format": "json", "formatversion": "2", "maxlag": "5", **params}
    while True:
        resp = client.get(_url, params=params)
        resp.raise_for_status()
        data = resp.json()
        err = data.get("error")
        if err and err.get("code") == "maxlag":
            wait = float(resp.headers.get("Retry-After", 5))
            log.info("server lagged, waiting %.0fs", wait)
            time.sleep(wait)
            continue
        if err:
            raise RuntimeError(f"API error: {err}")
        return data


# ---------------------------------------------------------------- 1. categories

def crawl_categories(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    apply_exclusions(con)                # the list may have changed since the last crawl
    excluded = set(_read_list(EXCLUDE_FILE))
    for seed in _read_list(SEEDS_FILE):
        con.execute("INSERT OR IGNORE INTO crawl_categories (source, title, depth) VALUES (?, ?, 0)",
                    (SOURCE, seed))
    con.commit()

    while True:
        row = con.execute("SELECT title, depth FROM crawl_categories WHERE source = ? AND state = 'pending' "
                          "ORDER BY depth, title LIMIT 1", (SOURCE,)).fetchone()
        if row is None:
            break
        title, depth = row["title"], row["depth"]
        if title in excluded:
            con.execute("UPDATE crawl_categories SET state = 'excluded' WHERE source = ? AND title = ?",
                        (SOURCE, title))
            con.commit()
            continue

        n_files = n_subcats = 0
        params = {"list": "categorymembers", "cmtitle": title, "cmtype": "subcat|file",
                  "cmprop": "title|type", "cmlimit": "500"}
        while True:
            data = api(client, **params)
            for m in data["query"]["categorymembers"]:
                if m["type"] == "subcat":
                    n_subcats += 1
                    if depth + 1 <= s.commons_max_depth:
                        state = "excluded" if m["title"] in excluded else "pending"
                        con.execute("INSERT OR IGNORE INTO crawl_categories (source, title, depth, parent, state) "
                                    "VALUES (?, ?, ?, ?, ?)", (SOURCE, m["title"], depth + 1, title, state))
                elif m["type"] == "file":
                    n_files += 1
                    con.execute("INSERT OR IGNORE INTO items (source, source_id) VALUES (?, ?)",
                                (SOURCE, m["title"]))
                    item_id = con.execute("SELECT id FROM items WHERE source = ? AND source_id = ?",
                                          (SOURCE, m["title"])).fetchone()[0]
                    con.execute("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                                (item_id, title))
            if "continue" not in data:
                break
            params.update(data["continue"])

        con.execute("UPDATE crawl_categories SET state = 'done', n_files = ?, n_subcats = ?, "
                    "done_at = datetime('now') WHERE source = ? AND title = ?",
                    (n_files, n_subcats, SOURCE, title))
        con.commit()
        log.info("[depth %d] %s: %d files, %d subcats", depth, title, n_files, n_subcats)


def apply_exclusions(con: sqlite3.Connection) -> None:
    """Re-apply commons_exclude.txt to what was already crawled (both ways, so it is reversible).

    A category is excluded if it, or an ancestor on its recorded parent chain, is in the list.
    An item whose categories are all excluded is not downloaded (status 'excluded'), or, if
    already stored, gets an 'excluded_category' flag. Items are never deleted.
    """
    roots = set(_read_list(EXCLUDE_FILE))
    rows = con.execute("SELECT title, parent, state, done_at FROM crawl_categories WHERE source = ?",
                       (SOURCE,)).fetchall()
    parent = {r["title"]: r["parent"] for r in rows}

    def excluded_by(title: str) -> str | None:
        seen = set()
        while title and title not in seen:
            if title in roots:
                return title
            seen.add(title)
            title = parent.get(title)
        return None

    cause = {r["title"]: excluded_by(r["title"]) for r in rows}
    for r in rows:
        if cause[r["title"]]:
            new_state = "excluded"
        elif r["state"] == "excluded":
            new_state = "done" if r["done_at"] else "pending"   # removed from the list: crawl it again
        else:
            continue
        con.execute("UPDATE crawl_categories SET state = ? WHERE source = ? AND title = ?",
                    (new_state, SOURCE, r["title"]))

    cats: dict[int, list[str]] = {}
    for r in con.execute("SELECT ic.item_id, ic.category FROM item_categories ic JOIN items i ON i.id = ic.item_id "
                         "WHERE i.source = ?", (SOURCE,)):
        cats.setdefault(r[0], []).append(r[1])
    status = dict(con.execute("SELECT id, status FROM items WHERE source = ?", (SOURCE,)).fetchall())
    n_ex = n_flag = n_back = 0
    for item_id, cs in cats.items():
        causes = [cause.get(c) for c in cs]
        all_excluded = all(causes)
        st = status.get(item_id)
        if all_excluded:
            reason = f"{cs[0]} (excluded via {causes[0]})"
            if st in ("listed", "meta_ok"):
                con.execute("UPDATE items SET status = 'excluded', error = ? WHERE id = ?", (reason, item_id))
                n_ex += 1
            elif st == "stored":
                n_flag += con.execute("INSERT OR IGNORE INTO item_flags (item_id, flag, reason) "
                                      "VALUES (?, 'excluded_category', ?)", (item_id, reason)).rowcount
        else:
            if st == "excluded":
                con.execute("UPDATE items SET status = CASE WHEN meta_at IS NULL THEN 'listed' ELSE 'meta_ok' END, "
                            "error = NULL WHERE id = ?", (item_id,))
                n_back += 1
            n_back += con.execute("DELETE FROM item_flags WHERE item_id = ? AND flag = 'excluded_category'",
                                  (item_id,)).rowcount
    con.commit()
    log.info("exclusions: %d items skipped before download, %d stored items flagged, %d restored",
             n_ex, n_flag, n_back)


# ---------------------------------------------------------------- 2. metadata

_TAG = re.compile(r"<[^>]+>")


def _text(value) -> str | None:
    """extmetadata values are HTML; multilingual ones are {lang: html}. Prefer en, then ja."""
    if isinstance(value, dict):
        langs = {k: v for k, v in value.items() if k != "_type"}
        value = langs.get("en") or langs.get("ja") or next(iter(langs.values()), None)
    if value is None:
        return None
    text = unescape(_TAG.sub("", str(value))).strip()
    return re.sub(r"\s+", " ", text) or None


def _meta(ext: dict, key: str):
    return (ext.get(key) or {}).get("value")


def fetch_metadata(s: Settings, con: sqlite3.Connection, client: PoliteClient, batch: int = 50) -> None:
    while True:
        rows = con.execute("SELECT id, source_id FROM items WHERE source = ? AND status = 'listed' "
                           "ORDER BY id LIMIT ?", (SOURCE, batch)).fetchall()
        if not rows:
            break
        by_title = {r["source_id"]: r["id"] for r in rows}
        data = api(client, titles="|".join(by_title), prop="imageinfo",
                   iiprop="url|size|mime|sha1|extmetadata|timestamp|user",
                   iiextmetadatamultilang="1")
        for page in data["query"]["pages"]:
            item_id = by_title.pop(page["title"], None)
            if item_id is None:
                continue
            if page.get("missing") or not page.get("imageinfo"):
                con.execute("UPDATE items SET status = 'missing', meta_at = datetime('now') WHERE id = ?",
                            (item_id,))
                continue
            _save_metadata(s, con, item_id, page)
        for title, item_id in by_title.items():   # not returned at all
            con.execute("UPDATE items SET status = 'error', error = 'no page in API response' WHERE id = ?",
                        (item_id,))
        con.commit()
        log.info("metadata: %d files", len(rows))


def _save_metadata(s: Settings, con: sqlite3.Connection, item_id: int, page: dict) -> None:
    info = page["imageinfo"][0]
    ext = info.get("extmetadata", {})
    mime = info.get("mime")
    status = "meta_ok"
    if mime not in MIME_EXT:
        status = "skipped_mime"
    elif info.get("size", 0) > s.max_file_bytes:
        status = "too_large"
    attribution = _text(_meta(ext, "AttributionRequired"))
    con.execute(
        """UPDATE items SET page_url = ?, file_url = ?, mime = ?, bytes = ?, width = ?, height = ?,
               source_sha1 = ?, title = ?, description = ?, author = ?, credit = ?, date_created = ?,
               license = ?, license_url = ?, attribution_required = ?, raw_json = ?, status = ?,
               meta_at = datetime('now')
           WHERE id = ?""",
        (info.get("descriptionurl"), info.get("url"), mime, info.get("size"),
         info.get("width"), info.get("height"), info.get("sha1"),
         _text(_meta(ext, "ObjectName")) or page["title"].removeprefix("File:"),
         _text(_meta(ext, "ImageDescription")),
         _text(_meta(ext, "Artist")),
         _text(_meta(ext, "Credit")),
         _text(_meta(ext, "DateTimeOriginal")),
         _text(_meta(ext, "LicenseShortName")) or "unknown",
         _text(_meta(ext, "LicenseUrl")),
         None if attribution is None else int(attribution == "true"),
         json.dumps(page, ensure_ascii=False),
         status, item_id))


# ---------------------------------------------------------------- 3. download

def download(s: Settings, con: sqlite3.Connection, client: PoliteClient, limit: int | None = None,
             minutes: float | None = None) -> None:
    """Download pending files; stop after `limit` files or `minutes` (between files, so nothing is cut off)."""
    deadline = time.monotonic() + minutes * 60 if minutes else None
    if n := ingest.retry_transient_errors(con, SOURCE):
        log.info("retrying %d files that failed for network reasons", n)
    done = 0
    while (limit is None or done < limit) and (deadline is None or time.monotonic() < deadline):
        row = con.execute("SELECT id, source_id, file_url, mime, source_sha1, width, height FROM items "
                          "WHERE source = ? AND status = 'meta_ok' ORDER BY id LIMIT 1", (SOURCE,)).fetchone()
        if row is None:
            break
        try:
            f = ingest.fetch(s, client, row["file_url"])
            if row["source_sha1"] and f.sha1 != row["source_sha1"]:
                f.path.unlink(missing_ok=True)
                raise ValueError("SHA-1 mismatch with Commons")
            sha256 = ingest.store_image(s, con, f, row["mime"], row["width"], row["height"])
            ingest.mark_stored(con, row["id"], sha256)
        except storage.StorageError:
            raise                      # disk problem: stop, don't mark items as failed
        except Exception as e:
            log.warning("failed %s: %s", row["source_id"], e)
            ingest.mark_error(con, row["id"], e)
        con.commit()
        done += 1
        if done % 50 == 0:
            log.info("downloaded %d files", done)
    log.info("download finished: %d files this run", done)
