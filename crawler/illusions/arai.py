"""錯視の科学館 / Science Museum of Visual Illusions (Hitoshi Arai and Shinobu Arai) — www.araiweb.matrix.jp.

The gallery is a frameset; its menu frame (Exhibition/illusiongallary3.html) links ~50 exhibition
pages (in Exhibition/ and sibling folders). Under each work image the caption reads
"Flower Garden Illusion No. 1 (Hitoshi Arai and Shinobu Arai, 2013)", which gives title, authors, year.
"""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site, page_title, text_after

SITE = "www.araiweb.matrix.jp"
RIGHTS = ("araiweb.matrix.jp: 「このページの著作権は新井仁之にあります．本ページに記載の内容・画像の一部あるいは全部の無断転載を"
          "禁じます．」「本サイトの画像，文章の全部，一部の無断転載，複製を禁止します．」")
CAPTIONS = [
    # "Flower Garden Illusion No. 1 (Hitoshi Arai and Shinobu Arai, 2013)", "Fig. 3-1. Spiral ... (4) (Arai-Arai, 2010)."
    re.compile(r"^(?P<title>.{2,150}?)\s*[(（](?P<who>[^()（）]*(?:Arai|ARAI|新井)[^()（）]*?)[,，、]\s*(?P<year>\d{4})[^()（）]*[)）]"),
    # "錯視の除去 ©新井仁之・新井しのぶ, 2010."
    re.compile(r"^(?P<title>.{1,80}?)\s*©\s*(?P<who>(?:新井|Hitoshi|Shinobu)[^,，、\d]*)[,，、]?\s*(?P<year>\d{4})"),
]
FIGURE = re.compile(r"^(?:図|Fig\.?|Figure)\s*[\d０-９]+[-.:．：]?\d*\s*(?P<title>[^。．.]{1,60})")
CHROME = re.compile(r"(^|/)(button|New\.gif|address|back|next|prev|home)", re.I)


class AraiMuseum(Site):
    source = "arai"
    start_urls = [f"http://{SITE}/Exhibition/illusiongallary3.html", f"http://{SITE}/Exhibition/museum.html"]
    hosts = {SITE: 1.0}
    license = "All rights reserved (Hitoshi Arai)"
    license_url = f"http://{SITE}/Exhibition/illusiongallary.html"
    flags = {"no_redistribution": RIGHTS}
    max_pages = 300
    max_depth = 1                         # the menu, then the exhibition pages it lists

    def in_scope(self, url: str) -> bool:
        return urlparse(url).netloc == SITE

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        if urlparse(page_url).path.endswith(("illusiongallary3.html",)):
            return []                     # the menu: only buttons
        page = page_title(tokens)
        date = re.search(r"(\d{4})年\s*\d{1,2}月\s*\d{1,2}日", html)
        found, seen = [], set()
        for i, t in enumerate(tokens):
            if t[0] != "img":
                continue
            url = urljoin(page_url, t[1])
            path = urlparse(url).path
            if url in seen or not IMAGE_EXT.search(path) or CHROME.search(path) or not self.in_scope(url):
                continue
            w, h = t[2].get("width", ""), t[2].get("height", "")
            if (w.isdigit() and int(w) < 60) or (h.isdigit() and int(h) < 60):
                continue
            seen.add(url)
            after = (text_after(tokens, i, limit=400) or "").replace("\xa0", " ").strip()
            m = next((c.match(after) for c in CAPTIONS if c.match(after)), None)
            fig = None if m else FIGURE.match(after)
            title = m.group("title").strip() if m else (fig.group(0).strip() if fig else page)
            found.append(Found(url, title=title, description=after or None,
                               author=m.group("who").strip() if m else "Hitoshi Arai, Shinobu Arai",
                               date=m.group("year") if m else (date.group(1) if date else None),
                               extra={"page_title": page, "caption_parsed": bool(m), "figure_caption": bool(fig)}))
        return found
