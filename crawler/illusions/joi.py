"""Journal of Illusion (open access, CC BY-NC-ND 4.0) — journalofillusion.net (OJS).

robots.txt asks for Crawl-delay: 60, so this runs slowly in chunks. Articles come from OAI-PMH;
for each article we read its landing page (citation_* meta, galley links), then the 'HTML Full Text'
galley, whose <img> figures (and still/animated frames of movies) each carry a caption paragraph
("Figure 1. ...", "Movie 1. ..."). Article state lives in crawl_pages; images go through register().
"""
import json
import logging
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from html import unescape
from urllib.parse import urljoin

from . import ingest, storage
from .client import PoliteClient
from .config import Settings
from .webcrawl import Found, Site, register

log = logging.getLogger(__name__)

SITE = "journalofillusion.net"
BASE = f"https://{SITE}/index.php/joi"


def _text(html: str | None) -> str | None:
    if not html:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip() or None


def _meta(html: str, name: str) -> list[str]:
    return [unescape(v) for v in re.findall(rf'<meta name="{name}" content="([^"]*)"', html)]


class JournalOfIllusion(Site):
    source = "joi"
    hosts = {SITE: 60.0}                        # robots.txt: Crawl-delay: 60
    ua_without_library = True                   # the server drops "python-requests" User-Agents
    license = "CC BY-NC-ND 4.0"
    license_url = "https://creativecommons.org/licenses/by-nc-nd/4.0/"
    flags = {}

    def in_scope(self, url: str) -> bool:
        return SITE in url

    def extract(self, page_url, html, tokens):
        return []

    def collect(self, s: Settings, con: sqlite3.Connection, client: PoliteClient, minutes: float | None) -> None:
        deadline = time.monotonic() + minutes * 60 if minutes else None
        if not con.execute("SELECT 1 FROM crawl_pages WHERE source = ? LIMIT 1", (self.source,)).fetchone():
            resp = client.get(f"{BASE}/oai", params={"verb": "ListRecords", "metadataPrefix": "oai_dc"})
            resp.raise_for_status()
            urls = sorted(set(re.findall(rf"<dc:identifier>({BASE}/article/view/\d+)</dc:identifier>", resp.text)))
            con.executemany("INSERT OR IGNORE INTO crawl_pages (source, url, depth) VALUES (?, ?, 0)",
                            [(self.source, u) for u in urls])
            con.commit()
            log.info("joi: %d articles listed via OAI-PMH", len(urls))

        while deadline is None or time.monotonic() < deadline:
            row = con.execute("SELECT url FROM crawl_pages WHERE source = ? AND state = 'pending' ORDER BY url LIMIT 1",
                              (self.source,)).fetchone()
            if row is None:
                break
            url, state, error, n = row["url"], "done", None, 0
            try:
                n = self._article(con, client, url)
            except Exception as e:
                state, error = "error", str(e)[:300]
                log.warning("joi: %s failed: %s", url, e)
            con.execute("UPDATE crawl_pages SET state = ?, error = ?, fetched_at = datetime('now') "
                        "WHERE source = ? AND url = ?", (state, error, self.source, url))
            con.commit()
            log.info("joi: %s -> %d figures", url.rsplit("/", 1)[-1], n)

    def _article(self, con: sqlite3.Connection, client: PoliteClient, url: str) -> int:
        resp = client.get(url)
        resp.raise_for_status()
        page = resp.text
        title = (_meta(page, "citation_title") or [None])[0]
        authors = ", ".join(_meta(page, "citation_author")) or None
        date = (_meta(page, "citation_date") or _meta(page, "citation_publication_date") or [None])[0]
        doi = (_meta(page, "citation_doi") or [None])[0]
        galley = re.search(r'<a[^>]+href="([^"]*/article/view/\d+/(\d+))"[^>]*>\s*HTML Full Text', page)
        if not galley:
            return 0                                   # PDF-only article: figures would need PDF extraction
        article_id = url.rsplit("/", 1)[-1]
        resp = client.get(f"{BASE}/article/download/{article_id}/{galley.group(2)}")
        resp.raise_for_status()
        html = resp.text
        n = 0
        for m in re.finditer(r'<img([^>]+)>(.*?)</p>', html, re.S):
            attrs, after = m.group(1), m.group(2)
            src = re.search(r'src="([^"]+)"', attrs)
            alt = (re.search(r'alt="([^"]*)"', attrs) or [None, ""])[1]
            if not src or alt == "symbol":             # ORCID icons
                continue
            caption = _text(after)
            label = re.match(r"((?:Figure|Fig\.|Movie|Table)\s*\d+[a-z]?)\.?", caption or "")
            f = Found(urljoin(resp.url, src.group(1)).split("?")[0],
                      title=f"{title} — {label.group(1)}" if label and title else title,
                      description=caption, author=authors, date=date,
                      extra={"doi": doi, "article": url, "alt": alt, "label": label.group(1) if label else None,
                             })
            n += register(con, self, url, title, f, sniff=True)
        return n


    # ------------------------------------------------------------ PDF-only articles

    def collect_pdfs(self, s: Settings, con: sqlite3.Connection, client: PoliteClient, minutes: float | None) -> None:
        """Articles without an HTML galley: extract the images embedded in the PDF (pdfimages)."""
        deadline = time.monotonic() + minutes * 60 if minutes else None
        with_html = {r[0] for r in con.execute("SELECT DISTINCT page_url FROM items WHERE source = ?", (self.source,))}
        done = {r[0] for r in con.execute("SELECT url FROM crawl_pages WHERE source = 'joi-pdf'")}
        todo = [r[0] for r in con.execute("SELECT url FROM crawl_pages WHERE source = ? AND state = 'done'", (self.source,))
                if r[0] not in with_html and r[0] not in done]
        for url in todo:
            if deadline and time.monotonic() > deadline:
                break
            state, error, n = "done", None, 0
            try:
                n = self._pdf(s, con, client, url)
            except storage.StorageError:
                raise
            except Exception as e:
                state, error = "error", str(e)[:300]
                log.warning("joi pdf: %s failed: %s", url, e)
            con.execute("INSERT OR REPLACE INTO crawl_pages (source, url, depth, state, error, fetched_at) "
                        "VALUES ('joi-pdf', ?, 0, ?, ?, datetime('now'))", (url, state, error))
            con.commit()
            log.info("joi pdf: %s -> %d images", url.rsplit("/", 1)[-1], n)

    def _pdf(self, s: Settings, con: sqlite3.Connection, client: PoliteClient, url: str) -> int:
        page = client.get(url).text
        title = (_meta(page, "citation_title") or [None])[0]
        authors = ", ".join(_meta(page, "citation_author")) or None
        date = (_meta(page, "citation_date") or _meta(page, "citation_publication_date") or [None])[0]
        doi = (_meta(page, "citation_doi") or [None])[0]
        pdf_link = (_meta(page, "citation_pdf_url") or [None])[0]
        if not pdf_link:
            return 0
        pdf_url = pdf_link.replace("/article/view/", "/article/download/")
        fetched = ingest.fetch(s, client, pdf_url, max_bytes=200 * 1024 * 1024)
        n = 0
        try:
            with tempfile.TemporaryDirectory(dir=s.tmp_dir) as tmp:
                # -png: write every embedded image as PNG; -p: put the page number in the file name
                subprocess.run(["pdfimages", "-png", "-p", str(fetched.path), f"{tmp}/img"], check=True,
                               capture_output=True, timeout=600)
                for f in sorted(Path(tmp).glob("img-*.png")):
                    page_no = int(f.name.split("-")[1])
                    data = f.read_bytes()
                    from PIL import Image
                    with Image.open(f) as im:
                        if max(im.size) < 100:            # icons, logos, rules
                            continue
                    source_id = f"{pdf_url}#{f.stem}"
                    cur = con.execute(
                        """INSERT OR IGNORE INTO items (source, source_id, page_url, file_url, mime, title, description,
                               author, date_created, license, license_url, attribution_required, raw_json, status, meta_at)
                           VALUES (?, ?, ?, ?, 'image/png', ?, ?, ?, ?, ?, ?, 1, ?, 'meta_ok', datetime('now'))""",
                        (self.source, source_id, url, pdf_url, f"{title} — PDF p.{page_no}", None, authors, date,
                         self.license, self.license_url,
                         json.dumps({"doi": doi, "pdf": pdf_url, "page": page_no, "extracted_by": "pdfimages"})))
                    if not cur.rowcount:
                        continue
                    sha = ingest.store_image(s, con, ingest.from_bytes(s, data), "image/png")
                    ingest.mark_stored(con, cur.lastrowid, sha)
                    n += 1
        finally:
            fetched.path.unlink(missing_ok=True)
        return n
