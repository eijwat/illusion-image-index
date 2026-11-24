"""Generic polite crawler for HTML sites; each site supplies a `Site` with scope rules and an extractor.

Flow (resumable, state in crawl_pages / items):
  crawl    — fetch pending pages in scope (robots.txt respected), queue links, register images found
  download — fetch registered images and store them (shared ingest code)
"""
import gzip
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from . import ingest, storage
from .client import PoliteClient
from .config import Settings

log = logging.getLogger(__name__)

IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|svg|webp|tiff?)$", re.I)
PAGE_EXT = re.compile(r"(/|\.html?)$", re.I)


@dataclass
class Found:
    """An image found on a page, with what the page says about it."""
    url: str
    title: str | None = None
    description: str | None = None
    author: str | None = None
    date: str | None = None
    extra: dict = field(default_factory=dict)


class Site:
    """Per-site rules. Subclasses set the class attributes and implement extract()."""
    source: str = ""
    start_urls: list[str] = []
    hosts: dict[str, float] = {}                 # host -> min seconds between requests
    license: str = "All rights reserved"
    license_url: str | None = None
    flags: dict[str, str] = {}                   # flag -> reason, put on every item of this site
    max_pages: int = 5000
    max_depth: int = 1000                        # link depth from the start URLs
    # Some servers drop any User-Agent containing "python-requests" (journalofillusion.net, 2026-10-05).
    # We still identify the bot and its contact; only the library suffix is left out for such sites.
    ua_without_library: bool = False

    def normalize(self, url: str) -> str:
        return urldefrag(url)[0]

    def in_scope(self, url: str) -> bool:
        raise NotImplementedError

    def follow(self, url: str) -> bool:
        """Whether to crawl this in-scope URL as a page (default: looks like HTML)."""
        return bool(PAGE_EXT.search(urlparse(url).path))

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        raise NotImplementedError

    def alternates(self, url: str) -> list[str]:
        """Other URLs to try when an image URL returns 404 (e.g. a mirror host)."""
        return []


# ---------------------------------------------------------------- HTML helpers

class _Tokenizer(HTMLParser):
    """Flatten a page into ('img', src, attrs) / ('a', href) / ('text', s) / ('hr',) / ('title', s) tokens."""
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "center"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "img" and a.get("src"):
            self.tokens.append(("img", a["src"].strip(), a))
        elif tag == "a" and a.get("href"):
            self.tokens.append(("a", a["href"].strip(), a))
        elif tag == "hr":
            self.tokens.append(("hr",))
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.tokens.append(("break",))

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.tokens.append(("break",))

    def handle_data(self, data):
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self.tokens.append(("title", text) if self._in_title else ("text", text))


def tokenize(html: str) -> list[tuple]:
    t = _Tokenizer()
    t.feed(html)
    return t.tokens


def decode(resp) -> str:
    """Decode bytes using the page's declared charset, falling back through common Japanese encodings."""
    raw = resp.content
    declared = []
    if m := re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I):
        declared.append(m.group(1).decode("ascii", "ignore"))
    if resp.encoding and "charset" in resp.headers.get("content-type", "").lower():
        declared.append(resp.encoding)
    for enc in declared + ["utf-8", "cp932", "euc-jp"]:
        try:
            return raw.decode(enc.replace("shift_jis", "cp932").replace("x-sjis", "cp932"))
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def page_title(tokens: list[tuple]) -> str | None:
    return next((t[1] for t in tokens if t[0] == "title"), None)


def text_after(tokens: list[tuple], i: int, limit: int = 300) -> str | None:
    """Text following token i, up to the next image or rule — a generic caption guess."""
    parts, n = [], 0
    for t in tokens[i + 1:]:
        if t[0] in ("img", "hr"):
            break
        if t[0] == "text":
            parts.append(t[1])
            n += len(t[1])
            if n >= limit:
                break
    return " ".join(parts)[:limit] or None


# ---------------------------------------------------------------- crawl

class Robots:
    def __init__(self, client: PoliteClient, user_agent: str):
        self.client, self.ua, self.parsers = client, user_agent, {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.parsers:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.client.get(base + "/robots.txt")
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except Exception as e:
                log.warning("robots.txt of %s unreadable (%s); treating as allow-all", base, e)
                rp.parse([])
            self.parsers[base] = rp
        return self.parsers[base].can_fetch(self.ua, url)


def cache_page(s: Settings, source: str, url: str, html: str) -> None:
    """Keep every crawled page (gzip, on the SSD) so extraction can be redone offline."""
    path = s.work_dir / "cache" / "pages" / source / (hashlib.sha1(url.encode()).hexdigest() + ".html.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(f"<!-- url: {url} -->\n{html}")


def reextract(s: Settings, con: sqlite3.Connection, site: Site) -> None:
    """Redo extraction from cached pages (no network) and update the metadata of known images."""
    n_pages = 0
    for path in sorted((s.work_dir / "cache" / "pages" / site.source).glob("*.html.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            first, html = f.readline(), f.read()
        url = first.removeprefix("<!-- url: ").removesuffix(" -->\n")
        tokens = tokenize(html)
        for found in site.extract(url, html, tokens):
            register(con, site, url, page_title(tokens), found, update=True)
        n_pages += 1
        if n_pages % 500 == 0:
            con.commit()
    con.commit()
    log.info("%s: re-extracted %d cached pages", site.source, n_pages)
    prefer_dated_pages(con, site)


def prefer_dated_pages(con: sqlite3.Connection, site: Site) -> None:
    """An image shown on several pages takes its metadata from a page that dates it (e.g. a Copyright
    line) rather than from an overview page like a catalog, where works are not separated."""
    n = 0
    for row in con.execute("SELECT id, raw_json, date_created FROM items WHERE source = ? AND date_created IS NULL "
                           "AND raw_json LIKE '%other_pages%'", (site.source,)).fetchall():
        raw = json.loads(row["raw_json"])
        best = next((o for o in raw.get("other_pages", []) if o.get("date")), None)
        if not best:
            continue
        primary = {k: raw.get(k) for k in ("page", "page_title", "title", "description", "author", "date")}
        raw["other_pages"] = [o for o in raw["other_pages"] if o is not best] + [primary]
        raw.update(best)
        con.execute("UPDATE items SET page_url = ?, title = ?, description = ?, author = ?, date_created = ?, "
                    "raw_json = ? WHERE id = ?", (best["page"], best["title"] or best.get("page_title"),
                                                  best["description"], best["author"], best["date"],
                                                  json.dumps(raw, ensure_ascii=False), row["id"]))
        n += 1
    con.commit()
    log.info("%s: %d images now described by a page that dates them", site.source, n)


def make_client(s: Settings, site: Site) -> PoliteClient:
    ua = s.user_agent.split(" python-requests/")[0] if site.ua_without_library else s.user_agent
    return PoliteClient(ua, site.hosts)


def crawl(s: Settings, con: sqlite3.Connection, client: PoliteClient, site: Site,
          minutes: float | None = None, refresh: bool = False) -> None:
    """Crawl pending pages. refresh=True re-reads every page already crawled ('stale') and updates the
    metadata of known images from it; a refresh in progress is continued by later plain runs."""
    deadline = time.monotonic() + minutes * 60 if minutes else None
    robots = Robots(client, s.user_agent)
    if refresh and not con.execute("SELECT 1 FROM crawl_pages WHERE source = ? AND state = 'stale' LIMIT 1",
                                   (site.source,)).fetchone():
        n = con.execute("UPDATE crawl_pages SET state = 'stale' WHERE source = ? AND state = 'done'",
                        (site.source,)).rowcount
        log.info("%s: refresh started for %d pages", site.source, n)
    for u in site.start_urls:
        con.execute("INSERT OR IGNORE INTO crawl_pages (source, url, depth) VALUES (?, ?, 0)",
                    (site.source, site.normalize(u)))
    con.commit()
    n_pages = n_images = 0
    while deadline is None or time.monotonic() < deadline:
        done = con.execute("SELECT count(*) FROM crawl_pages WHERE source = ? AND state NOT IN ('pending', 'stale')",
                           (site.source,)).fetchone()[0]
        if done >= site.max_pages:
            log.warning("%s: page cap %d reached", site.source, site.max_pages)
            break
        row = con.execute("SELECT url, depth, state FROM crawl_pages WHERE source = ? AND state IN ('pending', 'stale') "
                          "ORDER BY depth, url LIMIT 1", (site.source,)).fetchone()
        if row is None:
            break
        url, depth, update = row["url"], row["depth"], row["state"] == "stale"
        state, status, title, error = "done", None, None, None
        try:
            if not robots.allowed(url):
                state, error = "skipped", "robots.txt"
            else:
                resp = client.get(url)
                status = resp.status_code
                final = site.normalize(resp.url)
                if status != 200:
                    state, error = "error", f"HTTP {status}"
                elif "html" not in resp.headers.get("content-type", "html"):
                    state, error = "skipped", "not html"
                elif final != url and not site.in_scope(final):
                    state, error = "skipped", f"redirected out of scope: {final}"
                else:
                    html = decode(resp)
                    cache_page(s, site.source, final, html)
                    tokens = tokenize(html)
                    title = page_title(tokens)
                    for t in tokens:                       # queue links
                        if t[0] != "a":
                            continue
                        link = site.normalize(urljoin(final, t[1]))
                        if (link.startswith("http") and depth + 1 <= site.max_depth
                                and site.in_scope(link) and site.follow(link)):
                            con.execute("INSERT OR IGNORE INTO crawl_pages (source, url, depth, found_on) "
                                        "VALUES (?, ?, ?, ?)", (site.source, link, depth + 1, url))
                    for f in site.extract(final, html, tokens):
                        n_images += register(con, site, final, title, f, update=update)
        except Exception as e:
            state, error = "error", str(e)[:300]
            log.warning("%s: page %s failed: %s", site.source, url, e)
        con.execute("UPDATE crawl_pages SET state = ?, http_status = ?, title = ?, error = ?, "
                    "fetched_at = datetime('now') WHERE source = ? AND url = ?",
                    (state, status, title, error, site.source, url))
        con.commit()
        n_pages += 1
        if n_pages % 50 == 0:
            log.info("%s: %d pages crawled this run, %d new images", site.source, n_pages, n_images)
    log.info("%s: crawl stopped after %d pages, %d new images", site.source, n_pages, n_images)


def register(con: sqlite3.Connection, site: Site, page_url: str, page_title_: str | None, f: Found,
             update: bool = False, sniff: bool = False) -> int:
    """Insert an image item; if it is already known from another page, remember that page too.

    update=True (refresh): an existing item's title/description/date are replaced when this page is
    the one it was first found on, and this page's entry in other_pages is replaced otherwise.
    """
    mime = ingest.mime_from_name(urlparse(f.url).path)
    if not mime and not sniff:
        return 0                     # sniff=True: no extension in the URL; the type is read after download
    seen = {"page": page_url, "page_title": page_title_, "title": f.title, "description": f.description,
            "author": f.author, "date": f.date}
    existing = con.execute("SELECT id, raw_json FROM items WHERE source = ? AND source_id = ?",
                           (site.source, f.url)).fetchone()
    if existing:
        raw = json.loads(existing["raw_json"])
        others = raw.setdefault("other_pages", [])
        if update and page_url == raw.get("page"):
            raw.update(seen, extra=f.extra)
            con.execute("UPDATE items SET title = ?, description = ?, author = ?, date_created = ?, license = ?, "
                        "raw_json = ? WHERE id = ?",
                        (f.title or page_title_, f.description, f.author, f.date,
                         f.extra.get("license") or site.license, json.dumps(raw, ensure_ascii=False), existing["id"]))
            if "flags" in f.extra:                            # page-level flags follow the page
                con.execute("DELETE FROM item_flags WHERE item_id = ? AND flag = 'no_redistribution'",
                            (existing["id"],))
            _set_flags(con, existing["id"], site.flags | f.extra.get("flags", {}))
        elif page_url != raw.get("page"):
            known = [i for i, o in enumerate(others) if o["page"] == page_url]
            if known and update:
                others[known[0]] = seen
            elif not known and len(others) < 50:
                others.append(seen)
            con.execute("UPDATE items SET raw_json = ? WHERE id = ?",
                        (json.dumps(raw, ensure_ascii=False), existing["id"]))
        return 0
    raw = seen | {"extra": f.extra}
    cur = con.execute(
        """INSERT INTO items (source, source_id, page_url, file_url, mime, title, description, author,
               date_created, license, license_url, attribution_required, raw_json, status, meta_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'meta_ok', datetime('now'))""",
        (site.source, f.url, page_url, f.url, mime, f.title or page_title_, f.description, f.author,
         f.date, f.extra.get("license") or site.license,      # a page may state its own licence
         site.license_url, json.dumps(raw, ensure_ascii=False)))
    item_id = cur.lastrowid
    con.execute("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                (item_id, f"page:{page_url}"))
    _set_flags(con, item_id, site.flags | f.extra.get("flags", {}))
    return 1


def _set_flags(con: sqlite3.Connection, item_id: int, flags: dict[str, str]) -> None:
    for flag, reason in flags.items():
        con.execute("INSERT OR IGNORE INTO item_flags (item_id, flag, reason) VALUES (?, ?, ?)",
                    (item_id, flag, reason))


# ---------------------------------------------------------------- download

def download(s: Settings, con: sqlite3.Connection, client: PoliteClient, site: Site,
             minutes: float | None = None) -> None:
    deadline = time.monotonic() + minutes * 60 if minutes else None
    if n := ingest.retry_transient_errors(con, site.source):
        log.info("%s: retrying %d files that failed for network reasons", site.source, n)
    done = 0
    while deadline is None or time.monotonic() < deadline:
        row = con.execute("SELECT id, source_id, file_url, mime FROM items WHERE source = ? AND status = 'meta_ok' "
                          "ORDER BY id LIMIT 1", (site.source,)).fetchone()
        if row is None:
            break
        try:
            f, not_found = None, None
            for url in [row["file_url"]] + site.alternates(row["file_url"]):
                try:
                    f = ingest.fetch(s, client, url)
                    break
                except requests.HTTPError as e:
                    if e.response is None or e.response.status_code != 404:
                        raise
                    not_found = not_found or e        # report the original URL's 404
            if f is None:
                raise not_found
            if url != row["file_url"]:
                con.execute("UPDATE items SET file_url = ? WHERE id = ?", (url, row["id"]))
            mime = row["mime"] or ingest.sniff_mime(f.path)
            if not mime:
                f.path.unlink(missing_ok=True)
                raise ValueError("not a supported image")
            if not row["mime"]:
                con.execute("UPDATE items SET mime = ? WHERE id = ?", (mime, row["id"]))
            ingest.mark_stored(con, row["id"], ingest.store_image(s, con, f, mime))
        except storage.StorageError:
            raise
        except Exception as e:
            log.warning("%s: failed %s: %s", site.source, row["source_id"], e)
            ingest.mark_error(con, row["id"], e)
        con.commit()
        done += 1
        if done % 100 == 0:
            log.info("%s: downloaded %d files", site.source, done)
    log.info("%s: download stopped after %d files", site.source, done)
