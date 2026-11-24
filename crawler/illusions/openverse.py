"""Openverse (api.openverse.org): CC-licensed images from Flickr and other providers.

Registered application 'claude-illusions-bot' (2026-10-06); credentials in ~/.config/claude-illusions/openverse.json
(mode 600, never in the repository). Wikimedia is excluded (collected directly). Queries are generic illusion
words plus the English names of core concepts in our dictionary; the query that found an image is kept,
because broad words also return magic shows and the like.
"""
import json
import logging
import os
import sqlite3
import time

import requests

from .client import PoliteClient
from .config import Settings
from .webcrawl import Found, Site, register

log = logging.getLogger(__name__)

API = "https://api.openverse.org/v1"
CREDENTIALS = os.path.expanduser("~/.config/claude-illusions/openverse.json")
GENERIC_QUERIES = ["optical illusion", "visual illusion", "illusion art", "impossible object", "ambiguous figure",
                   "reversible figure", "trompe l'oeil", "anamorphosis", "op art", "moire pattern",
                   "afterimage", "illusory contour", "motion illusion", "color illusion", "geometric illusion",
                   "forced perspective", "pareidolia", "autostereogram"]
MAX_PAGES = 5                          # per query


class Openverse(Site):
    source = "openverse"
    hosts = {"api.openverse.org": 2.0}
    license = "per image (CC; see raw_json)"
    license_url = "https://openverse.org/"
    flags = {}

    def in_scope(self, url):
        return False

    def extract(self, page_url, html, tokens):
        return []

    def _token(self, s: Settings) -> str:
        cred = json.load(open(CREDENTIALS))
        r = requests.post(f"{API}/auth_tokens/token/", headers={"User-Agent": s.user_agent}, timeout=30,
                          data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                "client_secret": cred["client_secret"]})
        r.raise_for_status()
        return r.json()["access_token"]

    def collect(self, s: Settings, con: sqlite3.Connection, client: PoliteClient, minutes: float | None) -> None:
        deadline = time.monotonic() + minutes * 60 if minutes else None
        client.session.headers["Authorization"] = f"Bearer {self._token(s)}"
        names = [r[0] for r in con.execute("SELECT label_en FROM illusions WHERE core = 1 AND kind = 'phenomenon' "
                                           "AND label_en IS NOT NULL ORDER BY n_languages DESC")]
        queries = GENERIC_QUERIES + [n for n in names if n.lower() not in {q.lower() for q in GENERIC_QUERIES}]
        done = {r[0] for r in con.execute("SELECT url FROM crawl_pages WHERE source = ? AND state = 'done'", (self.source,))}
        n_new = 0
        for q in queries:
            if deadline and time.monotonic() > deadline:
                break
            key = f"query:{q}"
            if key in done:
                continue
            for page in range(1, MAX_PAGES + 1):
                resp = client.get(f"{API}/images/", params={"q": q, "page": page, "page_size": 50,
                                                            "excluded_source": "wikimedia", "mature": "false"})
                if resp.status_code in (400, 401) and page > 1:
                    break                                  # past the reachable pages
                resp.raise_for_status()
                data = resp.json()
                for r in data.get("results", []):
                    if not r.get("url"):
                        continue
                    lic = f"CC {r.get('license', '').upper()} {r.get('license_version') or ''}".strip()
                    if r.get("license") in ("cc0", "pdm"):
                        lic = "CC0" if r["license"] == "cc0" else "Public domain"
                    f = Found(r["url"], title=r.get("title"), description=r.get("attribution"),
                              author=r.get("creator"), date=None,
                              extra={"license": lic, "openverse_id": r.get("id"), "provider": r.get("provider"),
                                     "source": r.get("source"), "license_url": r.get("license_url"),
                                     "landing": r.get("foreign_landing_url"), "query": q,
                                     "tags": [t.get("name") for t in r.get("tags") or []][:30]})
                    n_new += register(con, self, r.get("foreign_landing_url") or r["url"], r.get("title"), f, sniff=True)
                    if "staticflickr.com" in r["url"]:
                        self._use_thumbnail(con, r["url"], r.get("id"))
                if page >= (data.get("page_count") or 1):
                    break
            con.execute("INSERT OR REPLACE INTO crawl_pages (source, url, depth, state, fetched_at) "
                        "VALUES (?, ?, 0, 'done', datetime('now'))", (self.source, key))
            con.commit()
        log.info("openverse: %d new images registered (%d queries)", n_new, len(queries))


    @staticmethod
    def _use_thumbnail(con: sqlite3.Connection, url: str, openverse_id: str) -> None:
        """Flickr's image CDN refuses crawlers ('Request forbidden by administrative rules', 2026-10-06);
        Openverse's own thumbnail endpoint (≤600 px) is the sanctioned route, so Flickr images use it."""
        con.execute("UPDATE items SET file_url = ?, raw_json = json_set(raw_json, '$.extra.thumbnail_only', json('true')) "
                    "WHERE source = 'openverse' AND source_id = ? AND status != 'stored'",
                    (f"{API}/images/{openverse_id}/thumb/", url))
