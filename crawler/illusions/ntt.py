"""NTT Communication Science Laboratories, Illusion Forum (illusion-forum.ilab.ntt.co.jp) — visual demos.

Each demo page (/<slug>/index.html) is a carousel of images (<slug>_1.png ...) with one caption per
step ("1 2本の水平線はどちらが長く見えますか?"), then an explanation (説明) that may contain figures.
Every image has 1x/2x/3x renditions in srcset; we take the largest. Video demos (mp4) are skipped.
Only the visual list is crawled (auditory / haptic demos are out of scope).
"""
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site

SITE = "illusion-forum.ilab.ntt.co.jp"
RIGHTS = ("© NTT. NTT copyright policy: personal use only, provided it is not made available to third parties or "
          "used commercially; any other use needs a written application.")


def _text(html: str | None) -> str | None:
    if not html:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip() or None


def _largest(img_tag: str, page_url: str) -> str | None:
    srcset = re.search(r'srcset="([^"]+)"', img_tag)
    if srcset:
        last = srcset.group(1).split(",")[-1].strip().split()[0]    # listed 1x, 2x, 3x
        return urljoin(page_url, last)
    src = re.search(r'src="([^"]+)"', img_tag)
    return urljoin(page_url, src.group(1)) if src else None


class IllusionForum(Site):
    source = "ntt"
    start_urls = [f"https://{SITE}/list-icon-visual.html"]
    hosts = {SITE: 1.0}
    license = "All rights reserved (NTT)"
    license_url = f"https://{SITE}/about.html"
    flags = {"no_redistribution": RIGHTS}
    max_pages = 200
    max_depth = 1                     # the visual list, then its demos; not demos linked from demos

    def in_scope(self, url: str) -> bool:
        return urlparse(url).netloc == SITE

    def follow(self, url: str) -> bool:
        # the visual list itself, and demo pages; not other lists (auditory, haptics, categories)
        path = urlparse(url).path
        return path == "/list-icon-visual.html" or re.fullmatch(r"/[\w-]+/(index\.html)?", path) is not None

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        if not re.fullmatch(r"/[\w-]+/(index\.html)?", urlparse(page_url).path):
            return []
        title = _text((re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1]) or ""
        parts = title.replace("Illusion Forum", "").replace("イリュージョンフォーラム", "").split()
        name = parts[-1] if parts else None
        category = parts[-2] if len(parts) >= 2 else None
        explain = re.search(r'id="demo-explain".*?<div class="textBox">(.*?)</div>', html, re.S)
        explanation = _text(explain.group(1))[:2000] if explain and _text(explain.group(1)) else None
        extra = {"category": category, "breadcrumb": parts[1:], "explanation": explanation}

        found, seen = [], set()
        for item in re.findall(r'<div class="carousel-item[^"]*"\s*>(.*?)</div>\s*</div>', html, re.S):
            img = re.search(r"<img[^>]+>", item)
            url = _largest(img.group(0), page_url) if img else None
            if not url or url in seen or not IMAGE_EXT.search(urlparse(url).path):
                continue
            seen.add(url)
            caption = _text((re.search(r'<div class="carousel-caption">(.*)$', item, re.S) or [None, ""])[1])
            found.append(Found(url, title=name, description=caption or explanation, author="NTT Illusion Forum",
                               extra=extra | {"step_caption": caption}))
        if explain:                                           # figures inside the explanation
            for tag in re.findall(r"<img[^>]+>", explain.group(1)):
                url = _largest(tag, page_url)
                if url and url not in seen and "/img/" not in urlparse(url).path and IMAGE_EXT.search(urlparse(url).path):
                    seen.add(url)
                    found.append(Found(url, title=name, description=explanation, author="NTT Illusion Forum",
                                       extra=extra | {"explanation_figure": True}))
        return found
