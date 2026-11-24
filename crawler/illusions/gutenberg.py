"""Project Gutenberg books (public-domain illusion figures).

Each book's HTML edition is fetched once; every figure image is stored with its caption
("Fig. N.—..."). Where the page shows a thumbnail linking to a larger image, the larger one is used.
"""
import json
import logging
import re
import sqlite3
from html import unescape
from urllib.parse import urljoin

from . import ingest, storage
from .client import PoliteClient
from .config import Settings

log = logging.getLogger(__name__)

SOURCE = "gutenberg"
HOST = "www.gutenberg.org"

BOOKS = {
    36297: {
        "title": "Visual Illusions: Their Causes, Characteristics and Applications",
        "author": "Matthew Luckiesh",
        "year": "1922",
        "html": "https://www.gutenberg.org/files/36297/36297-h/36297-h.htm",
        "skip": {"images/printer.jpg", "images/ad_image.jpg"},   # publisher's mark, advertisement
    },
}

# <a href="big.jpg"><img src="thumb.jpg"></a>  or  <img src="x.jpg">
IMG = re.compile(r'(?:<a\s+href="(?P<href>[^"]+\.(?:jpg|jpeg|png|gif))"[^>]*>\s*)?<img\s+src="(?P<src>[^"]+)"', re.I)
TAG = re.compile(r"<[^>]+>")


def make_client(s: Settings) -> PoliteClient:
    return PoliteClient(s.user_agent, {HOST: s.download_interval})


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG.sub(" ", html.replace("<br />", "\n")))).strip()


BLOCK = re.compile(r"<(p|td)\b[^>]*>(.*?)</\1>", re.S)
# The transcription is not uniform: "Fig. 42. The ..." lacks the dash, "33.—Reversible ..." lacks "Fig.".
FIG_CAPTION = re.compile(r"(?:Figs?\.\s*|^)(\d+)(?:\s*(?:and|,|to)\s*(\d+))?\.(?:\s*—)?\s*(.*?)(?=\s*Figs?\.\s*\d+|$)", re.S)


def _caption_index(html: str) -> dict[int, str]:
    """Figure number -> caption, from every 'Fig. N.—...' / 'Figs. N and M.—...' in paragraphs and table cells.

    Captions can sit under a single figure, under a pair of figures, or in a table row beside
    another figure, so they are looked up by the number in the image file name.
    """
    index: dict[int, str] = {}
    for block in BLOCK.finditer(html):
        text = _text(block.group(2))
        if not re.match(r"Figs?\.\s*\d|\d+\.\s*—", text):
            continue                                  # body text that merely mentions a figure
        for m in FIG_CAPTION.finditer(text):
            first, last = int(m.group(1)), int(m.group(2) or m.group(1))
            caption = m.group(0).strip()
            for n in range(first, last + 1):
                index.setdefault(n, caption)
    return index


def _figure_number(rel: str) -> int | None:
    m = re.search(r"fig(\d+)", rel.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else None


def _inline_context(html: str, start: int) -> str:
    """For an image inside running text, keep the surrounding sentence as its description."""
    p_start = html.rfind("<p>", 0, start)
    p_end = html.find("</p>", start)
    return _text(html[p_start:p_end])[:600]


def list_items(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    for book_id, book in BOOKS.items():
        resp = client.get(book["html"])
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="replace")
        captions = _caption_index(html)
        n = 0
        for m in IMG.finditer(html):
            rel = m.group("href") or m.group("src")
            if m.group("src") in book["skip"]:
                continue
            fig = _figure_number(rel)
            caption = captions.get(fig) if fig else None
            title = f"{book['title']} — Fig. {fig}" if fig else f"{book['title']} — {rel.rsplit('/', 1)[-1]}"
            description = caption or _inline_context(html, m.start())
            mime = ingest.mime_from_name(rel)
            raw = {"book_id": book_id, "book": {k: v for k, v in book.items() if k != "skip"},
                   "src": m.group("src"), "href": m.group("href"), "caption": caption}
            cur = con.execute(
                """INSERT OR IGNORE INTO items (source, source_id, page_url, file_url, mime, title, description,
                       author, date_created, license, license_url, attribution_required, raw_json, status, meta_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Public domain', ?, 0, ?, 'meta_ok', datetime('now'))""",
                (SOURCE, f"{book_id}/{rel}", f"https://www.gutenberg.org/ebooks/{book_id}",
                 urljoin(book["html"], rel), mime, title, description, book["author"], book["year"],
                 "https://www.gutenberg.org/policy/license.html", json.dumps(raw, ensure_ascii=False)))
            if cur.rowcount:
                con.execute("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                            (cur.lastrowid, f"book:{book_id}"))
                n += 1
        con.commit()
        log.info("gutenberg %d: %d new figures", book_id, n)


def download(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    ingest.retry_transient_errors(con, SOURCE)
    rows = con.execute("SELECT id, source_id, file_url, mime FROM items WHERE source = ? AND status = 'meta_ok' "
                       "ORDER BY id", (SOURCE,)).fetchall()
    for row in rows:
        try:
            f = ingest.fetch(s, client, row["file_url"])
            ingest.mark_stored(con, row["id"], ingest.store_image(s, con, f, row["mime"]))
        except storage.StorageError:
            raise
        except Exception as e:
            log.warning("failed %s: %s", row["source_id"], e)
            ingest.mark_error(con, row["id"], e)
        con.commit()
    log.info("gutenberg download finished: %d files", len(rows))
