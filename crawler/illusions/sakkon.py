"""錯視・錯聴コンテスト (Japanese Psychonomic Society), award-winning works 2009-.

Each year page (/akitaoka/sakkon/sakkonYYYY.html) has one table: award, title, authors,
affiliations, the work (images / video / pdf), explanation (pdf), thumbnail (often linking to
Visiome). We store the image files of each row; videos and PDFs are recorded as links only.
"""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site

RIGHTS = ("Contest pages: 「各作品の著作権は各作者にございます」. The contest rules only let the organizers use "
          "works for the contest web site, ceremony and publicity; no licence to third parties. "
          "Redistribution needs each author's permission.")

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)


def _text(html: str) -> str:
    html = re.sub(r"<sup>.*?</sup>", "", html, flags=re.S | re.I)      # affiliation numbers
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")).strip()


class Sakkon(Site):
    source = "sakkon"
    start_urls = ["https://www.psy.ritsumei.ac.jp/akitaoka/sakkon/sakkon2026.html"]
    hosts = {"www.psy.ritsumei.ac.jp": 2.0}      # same university server as Kitaoka: go slowly
    license = "All rights reserved (each author)"
    license_url = "https://www.psy.ritsumei.ac.jp/akitaoka/sakkon/sakkon2026.html"
    flags = {"no_redistribution": RIGHTS}
    max_pages = 100

    def normalize(self, url: str) -> str:
        return super().normalize(url).replace("http://", "https://", 1)

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == "www.psy.ritsumei.ac.jp" and p.path.startswith("/akitaoka/sakkon/")

    def follow(self, url: str) -> bool:
        # one page per year; the English and summary pages repeat the same works
        return re.fullmatch(r"/akitaoka/sakkon/sakkon\d{4}\.html", urlparse(url).path) is not None

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        year = re.search(r"sakkon(\d{4})\.html", page_url)
        rows = ROW.findall(html)
        if not rows:
            return []
        header = [_text(c) for c in CELL.findall(rows[0])]
        col = {name: next((i for i, h in enumerate(header) if h.startswith(name)), None)
               for name in ("賞の種類", "作品名", "作者名", "作者所属", "作品の説明")}
        if col["作品名"] is None:
            return []
        found = []
        for row in rows[1:]:
            cells = CELL.findall(row)
            if len(cells) <= col["作品名"]:
                continue
            get = lambda name: _text(cells[col[name]]) if col[name] is not None and col[name] < len(cells) else None
            title = (get("作品名") or "").strip("「」 ")
            award = re.sub(r"\s+", "", get("賞の種類") or "")
            links = [urljoin(page_url, u) for u in HREF.findall(row)]
            files = [u for u in links if re.search(r"\.(pdf|mp4|mov|mpe?g|avi|swf|wmv|pptx?|txt|zip)$", u, re.I)]
            visiome = [u for u in links if "visiome" in u]
            description = " / ".join(x for x in (f"{award}" if award else None,
                                                  f"所属: {get('作者所属')}" if get("作者所属") else None) if x)
            for u in dict.fromkeys(links):                   # images in the row, in order, once each
                if IMAGE_EXT.search(urlparse(u).path) and self.in_scope(u):
                    found.append(Found(u, title=title or None, description=description or None,
                                       author=get("作者名"), date=year.group(1) if year else None,
                                       extra={"award": award, "affiliation": get("作者所属"),
                                              "files": files, "visiome": visiome}))
        return found
