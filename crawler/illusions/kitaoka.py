"""Akiyoshi Kitaoka's illusion pages (Ritsumeikan University).

Pages list works one after another; each work is one or more images, a title in 「」,
an explanation, and a line 'Copyright Akiyoshi Kitaoka 2016 (March 20)' that closes it.
We cut the page at those lines to pair images with their title, text and date.
"""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site

COPYRIGHT = re.compile(r"Copyright\s+(?:\(c\)\s*|©\s*)?Akiyoshi\s+Kitaoka\s*,?\s*(\d{4})(?:\s*\(([^)]*)\))?", re.I)
WHOLE_TITLE = re.compile(r"「([^」]{1,120})」.{0,12}|[\"“]\s*([^\"”]{2,120})[\"”]")

# link captions that are not work titles ("see", "high-resolution figure", ...)
LINK_WORDS = {"見る", "高解像度の図", "大きい図", "もっと大きい図", "小さい図", "高解像度", "拡大", "拡大図", "ダウンロード", "high-resolution image",
              "high resolution", "Download", "download", "Enlarge", "enlarge", "here", "ここ", "→", "PDF"}

# A single work rarely has more than a handful of images (variants, sizes); 99% of Copyright-closed
# blocks have <= 12. Larger blocks are overview pages mixing many works (2026-10-04 measurement).
MAX_WORK_IMAGES = 12

DISCLAIMER = ("Kitaoka DISCLAIMER (index-e.html, checked 2026-10-02): not copyright-free; research/personal/educational "
              "use only with citation; commercial use via publisher Kanzen; up to 3 images on a non-commercial "
              "illusion-science web page with name and URL. Redistribution in a public DB needs the author's permission.")


def _title(lines: list[str]) -> str | None:
    """A work title: a line that is entirely 「...」 (or "..."), else the first short line.

    Only whole-line quotes count: 「」 inside a sentence is often a book title or a quoted word.
    """
    for s in lines:
        if m := WHOLE_TITLE.fullmatch(s.strip()):
            return (m.group(1) or m.group(2)).strip()
    return next((s for s in lines if len(s) <= 60 and s not in LINK_WORDS and not COPYRIGHT.search(s)), None)


class Kitaoka(Site):
    source = "kitaoka"
    start_urls = ["https://www.ritsumei.ac.jp/~akitaoka/",
                  "https://www.psy.ritsumei.ac.jp/akitaoka/",
                  "https://www.psy.ritsumei.ac.jp/akitaoka/index-e.html"]
    hosts = {"www.ritsumei.ac.jp": 2.0, "www.psy.ritsumei.ac.jp": 2.0}   # a university server: go slowly (2026-10-04)
    license = "All rights reserved (research/educational use with citation)"
    license_url = "https://www.psy.ritsumei.ac.jp/akitaoka/index-e.html"
    flags = {"no_redistribution": DISCLAIMER}
    max_pages = 4000

    def normalize(self, url: str) -> str:
        url = super().normalize(url).replace("http://", "https://", 1)
        # /~akitaoka/ redirects to /akitaoka/ on the psy host; treat them as one page
        return url.replace("www.psy.ritsumei.ac.jp/~akitaoka/", "www.psy.ritsumei.ac.jp/akitaoka/")

    def alternates(self, url: str) -> list[str]:
        # pages on www.ritsumei.ac.jp/~akitaoka/ reference files that only exist on the psy host
        if "www.ritsumei.ac.jp/~akitaoka/" in url:
            return [url.replace("www.ritsumei.ac.jp/~akitaoka/", "www.psy.ritsumei.ac.jp/akitaoka/")]
        return []

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        if p.netloc == "www.psy.ritsumei.ac.jp":
            # the contest pages (/akitaoka/sakkon/) are other authors' works: a separate source
            return p.path.startswith("/akitaoka/") and not p.path.startswith("/akitaoka/sakkon/")
        return p.netloc == "www.ritsumei.ac.jp" and p.path.startswith("/~akitaoka/")

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        """Cut the page into works. A work ends at its Copyright line, or at a horizontal rule.

        The explanation is the text between the work's last image and its end, so text of the
        previous work (or of the page header) is not attached to it.
        """
        found: list[Found] = []
        imgs: list[tuple[int, str]] = []            # images of the current work: (token index, url)
        before: list[str] = []                      # text since the work began (before/among images)
        after: list[str] = []                       # text since the work's last image

        def take_image(i: int, src: str, attrs: dict) -> None:
            url = self.normalize(urljoin(page_url, src))
            if "cgi-bin" in url or not IMAGE_EXT.search(urlparse(url).path) or not self.in_scope(url):
                return
            w, h = attrs.get("width", ""), attrs.get("height", "")
            if (w.isdigit() and int(w) < 40) or (h.isdigit() and int(h) < 40):
                return                                   # icons, bullets, counters
            before.extend(after)
            after.clear()
            imgs.append((i, url))

        def close(copyright_match=None) -> None:
            text = " ".join(after)
            name = _title(after) or _title(before[::-1])   # before the image: nearest line first
            extra = {"copyright": copyright_match.group(0)} if copyright_match else {"no_copyright_line": True}
            date = None
            if copyright_match:
                date = copyright_match.group(1) + (f" ({copyright_match.group(2)})" if copyright_match.group(2) else "")
            if len(imgs) > MAX_WORK_IMAGES:
                # overview pages (catalog, lecture notes) put many works under one Copyright line:
                # don't attribute that one title/date to all of them
                extra = {"merged_block": len(imgs), "block_text": text[:1500]}
                name, date, text, copyright_match = None, None, "", None
            for _, url in imgs:
                found.append(Found(url, title=name,
                                   description=text[:1500] or None,
                                   author="Akiyoshi Kitaoka" if copyright_match else None,
                                   date=date, extra=extra))
            imgs.clear()
            before.clear()
            after.clear()

        for i, t in enumerate(tokens):
            kind = t[0]
            if kind == "img":
                take_image(i, t[1], t[2])
            elif kind == "a" and IMAGE_EXT.search(urlparse(t[1]).path):
                take_image(i, t[1], {})                  # link to a larger version
            elif kind == "hr":
                if imgs:
                    close()
                before.clear()
                after.clear()
            elif kind == "text":
                m = COPYRIGHT.search(t[1])
                if m and imgs:
                    head = t[1][:m.start()].strip()
                    if head:
                        after.append(head)
                    close(m)
                elif not m:
                    after.append(t[1])
        if imgs:
            close()
        seen, unique = set(), []
        for f in found:
            if f.url not in seen:
                seen.add(f.url)
                unique.append(f)
        return unique
