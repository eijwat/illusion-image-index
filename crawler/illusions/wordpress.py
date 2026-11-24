"""WordPress sites via the REST API (/wp-json/wp/v2/): no page-by-page crawling.

For a site we list all posts (title, date, link, categories, tags, content) and the media
library (original file URLs, parent post). Each media file becomes an item described by its
parent post; images embedded in post content but missing from the media library (older posts
often hotlink) are added too. Download and storage reuse the HTML-site code (webcrawl).
"""
import json
import logging
import re
import sqlite3
from html import unescape
from urllib.parse import urljoin, urlparse

from .client import PoliteClient
from .config import Settings
from .webcrawl import IMAGE_EXT, Found, Site, register

log = logging.getLogger(__name__)

PHOTOBUCKET_WATERMARK = ("Photobucket serves hotlinked images with a large 'Groupsy by photobucket' "
                         "overlay (checked 2026-10-05): the picture is real but watermarked.")
SIZE_SUFFIX = re.compile(r"-\d+x\d+(?=\.\w+$)")      # WordPress resized rendition: name-300x200.jpg


HOST_FIRST = re.compile(r"^(?:[\w-]+\.)+[a-z]{2,6}(?::\d+)?/", re.I)


def fix_scheme_less(src: str) -> str:
    """'i207.photobucket.com/a.jpg' (no scheme, common in old posts) is an external URL, not a site path."""
    src = src.strip()
    if HOST_FIRST.match(src) and not src.startswith(("http:", "https:", "//", "/")):
        return "http://" + src
    return src


def _text(html: str | None) -> str | None:
    if not html:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip() or None


class WordPressSite(Site):
    base = ""                         # https://example.com (the WordPress root, e.g. https://x.org/blog)
    fields_post = "id,date,link,title,content,excerpt,categories,tags"
    with_pages = False                # some sites put their galleries on pages, not posts

    def in_scope(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.base).netloc

    def extract(self, page_url, html, tokens):          # not used: everything comes from the API
        return []

    # ------------------------------------------------------------ API

    def _all(self, client: PoliteClient, endpoint: str, **params) -> list[dict]:
        out, page = [], 1
        while True:
            resp = client.get(f"{self.base}/wp-json/wp/v2/{endpoint}",
                              params={"per_page": 100, "page": page, **params})
            if resp.status_code == 400 and page > 1:    # past the last page
                break
            resp.raise_for_status()
            batch = resp.json()
            out.extend(batch)
            total_pages = int(resp.headers.get("X-WP-TotalPages", page))
            if page >= total_pages or not batch:
                break
            page += 1
        return out

    def collect(self, s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
        cache = s.work_dir / "cache" / "wordpress" / self.source
        cache.mkdir(parents=True, exist_ok=True)
        terms = {}
        for kind in ("categories", "tags"):
            got = self._all(client, kind, _fields="id,name")
            terms[kind] = {t["id"]: unescape(t["name"]) for t in got}
        posts = self._all(client, "posts", _fields=self.fields_post)
        if self.with_pages:
            posts += self._all(client, "pages", _fields="id,date,link,title,content,excerpt")
        media = self._all(client, "media", _fields="id,date,post,source_url,mime_type,title,caption,alt_text")
        (cache / "posts.json").write_text(json.dumps(posts, ensure_ascii=False))
        (cache / "media.json").write_text(json.dumps(media, ensure_ascii=False))
        log.info("%s: %d posts, %d media, %d categories, %d tags", self.source, len(posts), len(media),
                 len(terms["categories"]), len(terms["tags"]))

        by_id = {p["id"]: p for p in posts}

        def describe(p: dict | None) -> dict:
            if not p:
                return {}
            cats = [terms["categories"].get(c) for c in p.get("categories", [])]
            tags = [terms["tags"].get(t) for t in p.get("tags", [])]
            return {"title": _text(p["title"]["rendered"]), "page": p["link"], "date": p["date"][:10],
                    "description": (_text(p["content"]["rendered"]) or _text(p["excerpt"]["rendered"]) or "")[:1500] or None,
                    "categories": [c for c in cats if c], "tags": [t for t in tags if t]}

        n = 0
        in_library = set()
        for m in media:
            url = m["source_url"]
            in_library.add(SIZE_SUFFIX.sub("", url))
            if not (m.get("mime_type") or "").startswith("image/"):
                continue
            d = describe(by_id.get(m.get("post")))
            n += self._add(con, url, d, {"media_id": m["id"], "caption": _text(m["caption"]["rendered"]),
                                         "alt": m.get("alt_text"), "media_title": _text(m["title"]["rendered"])})
        for p in posts:                                   # images only embedded in content
            d = describe(p)
            for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""", p["content"]["rendered"]):
                url = urljoin(p["link"], fix_scheme_less(src))
                original = SIZE_SUFFIX.sub("", url)
                if original in in_library or not IMAGE_EXT.search(urlparse(url).path):
                    continue
                n += self._add(con, url, d, {"embedded": True})
        con.commit()
        log.info("%s: %d new images registered", self.source, n)

    def _add(self, con: sqlite3.Connection, url: str, d: dict, extra: dict) -> int:
        page = d.get("page") or self.base
        if urlparse(url).netloc.endswith("photobucket.com"):
            extra = extra | {"flags": {"watermarked": PHOTOBUCKET_WATERMARK}}
        f = Found(url, title=d.get("title") or extra.get("media_title"), description=d.get("description"),
                  author=None, date=d.get("date"),
                  extra=extra | {"categories": d.get("categories", []), "tags": d.get("tags", [])})
        added = register(con, self, page, d.get("title"), f)
        if added:
            item_id = con.execute("SELECT id FROM items WHERE source = ? AND source_id = ?",
                                  (self.source, url)).fetchone()[0]
            con.executemany("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                            [(item_id, f"category:{c}") for c in d.get("categories", [])] +
                            [(item_id, f"tag:{t}") for t in d.get("tags", [])])
        return added


# ---------------------------------------------------------------- sites

class MightyOpticalIllusions(WordPressSite):
    source = "moillusions"
    base = "https://moillusions.com"
    hosts = {"moillusions.com": 1.0}
    license = "All rights reserved (site; many images reposted from elsewhere, original authors often unknown)"
    license_url = "https://moillusions.com/"
    flags = {"no_redistribution": "moillusions.com: '© Mighty Optical Illusions'; most images are reposts with "
                                  "unclear original rights. Publish metadata and links only."}


class Illusionen(WordPressSite):
    source = "illusionen"
    base = "https://www.illusionen.org/blog"
    with_pages = True                 # 22 category gallery pages hold the images; only 5 posts
    hosts = {"www.illusionen.org": 1.0}
    license = "All rights reserved (operator's works: private/non-commercial use only; some images CC BY-SA)"
    license_url = "https://www.illusionen.org/blog/impressum/"
    flags = {"no_redistribution": "illusionen.org Impressum: works by the operator are protected under German "
                                  "copyright law; private/non-commercial use only, otherwise written permission. "
                                  "Some images are CC BY-SA (check per image)."}


class ArchimedesLab(WordPressSite):
    source = "archimedeslab"
    base = "https://archimedes-lab.org"
    hosts = {"archimedes-lab.org": 1.0}
    license = "All rights reserved (Gianni A. Sarcone; licences sold)"
    license_url = "https://archimedes-lab.org/"
    flags = {"no_redistribution": "archimedes-lab.org: 'All images and content are protected by copyright'; "
                                  "the author sells licences. Includes puzzle articles that are not illusions."}


class AnstisLab(WordPressSite):
    source = "anstis"
    base = "https://anstislab.ucsd.edu"
    with_pages = True
    hosts = {"anstislab.ucsd.edu": 1.0}
    license = "All rights reserved (no license stated)"
    license_url = "https://anstislab.ucsd.edu/illusions/"
    flags = {"no_redistribution": "anstislab.ucsd.edu states no license: treat as all rights reserved."}


class IllusionScience(WordPressSite):
    source = "shapiro"
    base = "https://illusionscience.com"
    with_pages = True
    hosts = {"illusionscience.com": 1.0}
    license = "unclear (page shows CC BY-NC-SA 4.0 and also 'no unauthorized use')"
    license_url = "https://illusionscience.com/"
    flags = {"no_redistribution": "illusionscience.com (Arthur Shapiro) shows a CC BY-NC-SA 4.0 notice and an "
                                  "all-rights statement on the same page; until clarified, treat as not redistributable."}


class IllusionOfTheYear(WordPressSite):
    source = "ioty"
    base = "https://illusionoftheyear.com"
    with_pages = True
    hosts = {"illusionoftheyear.com": 1.0}
    license = "All rights reserved (Neural Correlate Society; each entry by its authors)"
    license_url = "https://illusionoftheyear.com/"
    flags = {"no_redistribution": "illusionoftheyear.com: '© Neural Correlate Society, All Rights Reserved'; "
                                  "entries belong to their authors. Most entries are videos (YouTube); we keep images only."}


class OpticalIllusionNet(WordPressSite):
    source = "opticalillusionnet"
    base = "https://www.opticalillusion.net"
    hosts = {"www.opticalillusion.net": 1.0}
    license = "Private/non-commercial use of still images allowed (third-party works excluded)"
    license_url = "https://www.opticalillusion.net/"
    flags = {"no_redistribution": "opticalillusion.net allows private, non-commercial use of its still images "
                                  "(not third-party works); redistribution in a public DB is not granted."}


class IllusionWorks(WordPressSite):
    source = "illusionworks"
    base = "https://www.illusionworks.com"
    with_pages = True
    hosts = {"www.illusionworks.com": 1.0}
    license = "All rights reserved (no licence stated)"
    license_url = "https://www.illusionworks.com/"
    flags = {"no_redistribution": "illusionworks.com: no licence stated; treat as all rights reserved."}


class KodakaLab(WordPressSite):
    source = "kodaka"
    base = "https://lab.kenrikodaka.com"
    with_pages = True
    hosts = {"lab.kenrikodaka.com": 1.0}
    license = "All rights reserved (小鷹研究室as)"
    license_url = "https://lab.kenrikodaka.com/"
    flags = {"no_redistribution": "lab.kenrikodaka.com: 'Copyright © 小鷹研究室as. All Rights Reserved.' "
                                  "Mostly body illusions shown as video/installations; images only."}
