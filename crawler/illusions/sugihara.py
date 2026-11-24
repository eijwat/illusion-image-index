"""Kokichi Sugihara (Meiji University) — impossible objects, ambiguous cylinders and other 3D illusions.

The Japanese top page links one page per series (…j.html). On a series page each work is a photo
followed by its title in 「」 and an explanation. Many works are three-dimensional objects whose
effect shows best in motion (YouTube videos, not collected); the photos are what we store.
"""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site, page_title, text_after

SITE = "www.isc.meiji.ac.jp"
ROOT = "/~kokichis/"
RIGHTS = ("No licence stated on Sugihara's pages (3D-printer data is offered for non-commercial use with "
          "attribution, which does not cover the photos). Treat as all rights reserved.")
TITLE = re.compile(r"^[^「]{0,20}「([^」]{1,80})」")
YEAR = re.compile(r"(20\d\d|19\d\d)\s*年")


class Sugihara(Site):
    source = "sugihara"
    start_urls = [f"http://{SITE}{ROOT}Welcomej.html"]
    hosts = {SITE: 1.0}
    license = "All rights reserved (no licence stated)"
    license_url = f"http://{SITE}{ROOT}Welcomej.html"
    flags = {"no_redistribution": RIGHTS}
    max_pages = 100
    max_depth = 1                         # the top page, then one page per series

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == SITE and p.path.startswith(ROOT)

    def follow(self, url: str) -> bool:
        return urlparse(url).path.endswith("j.html")       # Japanese pages; English ones repeat the photos

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        if urlparse(page_url).path.endswith("Welcomej.html"):
            return []
        # the series heading: the first short lines of the page, before the explanation starts
        heading = []
        for t in tokens:
            if t[0] == "img" or (t[0] == "text" and len(t[1]) > 40 and heading):
                break
            if t[0] == "text" and "Homepage" not in t[1] and "トップページへ" not in t[1] and len(t[1]) <= 40:
                heading.append(t[1])
        series = " ".join(heading)[:80] or page_title(tokens)
        found, seen = [], set()
        for i, t in enumerate(tokens):
            if t[0] != "img":
                continue
            url = urljoin(page_url, t[1])
            if url in seen or not IMAGE_EXT.search(urlparse(url).path) or not self.in_scope(url):
                continue
            seen.add(url)
            after = text_after(tokens, i, limit=600) or ""
            title = TITLE.match(after)
            year = YEAR.search(after)
            found.append(Found(url, title=title.group(1) if title else series, description=after or None,
                               author="Kokichi Sugihara", date=year.group(1) if year else None,
                               extra={"series": series}))
        return found
