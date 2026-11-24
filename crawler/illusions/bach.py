"""Michael Bach, 'Optical Illusions & Visual Phenomena' (michaelbach.de/ot/).

One illusion per page (/ot/<slug>/index.html): the <h1> is its name, the paragraphs explain it.
Images are the <img> tags plus image files named in the page's rollover scripts.
Many demos are JavaScript/canvas and have no image file; those pages yield nothing to store.
"""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site

# robots.txt of michaelbach.de, checked 2026-10-04 (Cloudflare-managed block):
CONTENT_SIGNAL = "search=yes, ai-input=no, ai-train=yes"
NO_AI_INPUT = (f"michaelbach.de robots.txt Content-signal: {CONTENT_SIGNAL} (checked 2026-10-04). "
               "ai-input=no: must not be input into AI models (RAG, grounding, real-time generative answers). "
               "Declared as an express reservation of rights under Art. 4 of EU Directive 2019/790.")
NO_REDISTRIBUTION = ("No license stated on michaelbach.de: treat as all rights reserved; publishing images "
                     "needs permission (michael.bach@uni-freiburg.de).")

SCRIPT_IMAGE = re.compile(r"""['"]([\w./-]+\.(?:gif|png|jpe?g|svg|webp))['"]""", re.I)
SKIP_IMAGES = {"icon.gif"}                       # 64px thumbnail of the page's own illusion


class Bach(Site):
    source = "bach"
    start_urls = ["https://michaelbach.de/ot/"]
    hosts = {"michaelbach.de": 1.0}
    license = "All rights reserved (no license stated)"
    license_url = "https://michaelbach.de/ot/"
    flags = {"no_ai_input": NO_AI_INPUT, "no_redistribution": NO_REDISTRIBUTION}
    max_pages = 600

    def normalize(self, url: str) -> str:
        url = super().normalize(url).replace("http://", "https://", 1)
        return url[:-len("index.html")] if url.endswith("/index.html") else url

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == "michaelbach.de" and p.path.startswith("/ot/") and "/-misc/" not in p.path

    def follow(self, url: str) -> bool:
        # the index and one page per illusion; skip German duplicates (index-de.html) and other files
        path = urlparse(url).path
        return path == "/ot/" or re.fullmatch(r"/ot/[^/]+/", path) is not None

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        if urlparse(page_url).path == "/ot/":
            return []
        name = next((t[1] for t in tokens if t[0] == "text"), None)
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        if h1:
            name = re.sub(r"<[^>]+>|\s+", " ", h1.group(1)).strip()
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
        text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in paragraphs)
        text = re.sub(r"\s+", " ", text).strip()[:2000] or None

        srcs = [t[1] for t in tokens if t[0] == "img"] + SCRIPT_IMAGE.findall(html)
        found, seen = [], set()
        for src in srcs:
            url = urljoin(page_url, src)
            path = urlparse(url).path
            if (not IMAGE_EXT.search(path) or not self.in_scope(url) or url in seen
                    or path.rsplit("/", 1)[-1] in SKIP_IMAGES):
                continue
            seen.add(url)
            found.append(Found(url, title=name, description=text, author="Michael Bach (site)",   # the page credits the original discoverer in the text
                               extra={"slug": path.split("/")[2]}))
        return found
