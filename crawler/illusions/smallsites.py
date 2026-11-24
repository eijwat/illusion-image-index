"""Small HTML sites handled by the generic crawler with an image + following-text caption."""
import re
from urllib.parse import urljoin, urlparse

from .webcrawl import IMAGE_EXT, Found, Site, page_title, text_after


class NovickColorIllusions(Site):
    """David Novick's color illusions (UTEP): ~19 galleries, some spread over several pages."""
    source = "novick"
    start_urls = ["https://www.utep.edu/engineering/novick/colors/colorillusions.html"]
    hosts = {"www.utep.edu": 1.0}
    license = "All rights reserved (no licence stated)"
    license_url = "https://www.utep.edu/engineering/novick/colors/colorillusions.html"
    flags = {"no_redistribution": "No licence stated on David Novick's color illusion pages: treat as all rights reserved."}
    max_pages = 200
    ROOT = "/engineering/novick/colors/"
    CHROME = re.compile(r"/images/|_colleges|warning\.svg|logo", re.I)

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == "www.utep.edu" and p.path.startswith(self.ROOT)

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        title = page_title(tokens)
        found, seen = [], set()
        for i, t in enumerate(tokens):
            if t[0] != "img":
                continue
            url = urljoin(page_url, t[1])
            if url in seen or not IMAGE_EXT.search(urlparse(url).path) or self.CHROME.search(url) or not self.in_scope(url):
                continue
            seen.add(url)
            found.append(Found(url, title=title, description=text_after(tokens, i, limit=400),
                               author="David Novick"))
        return found


class Gallery(Site):
    """A small gallery: pages under one URL prefix, each image described by the text right after it."""
    prefix = ""                       # https://host/path/ — pages and images must start with this
    image_prefix = None               # images may live elsewhere on the host (default: same host)
    author = None
    CHROME = re.compile(r"logo|banner|button|icon|spacer|blank|arrow|/ad[s_/]|counter|cgi-bin", re.I)

    def in_scope(self, url: str) -> bool:
        return url.startswith(self.prefix)

    def _image_ok(self, url: str) -> bool:
        if self.image_prefix is not None:
            return url.startswith(self.image_prefix)
        return urlparse(url).netloc == urlparse(self.prefix).netloc

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        title = page_title(tokens)
        found, seen = [], set()
        for i, t in enumerate(tokens):
            if t[0] != "img":
                continue
            url = urljoin(page_url, t[1])
            w, h = t[2].get("width", ""), t[2].get("height", "")
            if (url in seen or not IMAGE_EXT.search(urlparse(url).path) or self.CHROME.search(url)
                    or not self._image_ok(url) or (w.isdigit() and int(w) < 40) or (h.isdigit() and int(h) < 40)):
                continue
            seen.add(url)
            alt = (t[2].get("alt") or "").strip()
            found.append(Found(url, title=alt if 3 <= len(alt) <= 100 else title,
                               description=text_after(tokens, i, limit=400), author=self.author,
                               extra={"page_title": title, "alt": alt or None}))
        return found


UNKNOWN = "No licence stated: treat as all rights reserved."


class Panoptikum(Gallery):
    source = "panoptikum"
    prefix = "https://www.panoptikum.net/optischetaeuschungen/"
    start_urls = [prefix]
    hosts = {"www.panoptikum.net": 1.0}
    license = "All rights reserved (artists credited; some GNU FDL)"
    license_url = prefix
    flags = {"no_redistribution": "panoptikum.net: works by credited artists (e.g. Sandro del Prete); only a few "
                                  "images state GNU FDL. " + UNKNOWN}
    max_pages = 200
    max_depth = 2


class EyeTricks(Gallery):
    source = "eyetricks"
    prefix = "http://www.eyetricks.com/"
    start_urls = ["http://www.eyetricks.com/illusions.htm"]
    hosts = {"www.eyetricks.com": 1.0}
    license = "All rights reserved (no licence stated)"
    license_url = "http://www.eyetricks.com/illusions.htm"
    flags = {"no_redistribution": "eyetricks.com: " + UNKNOWN}
    max_pages = 300
    max_depth = 2


class Benricho(Gallery):
    source = "benricho"
    prefix = "https://www.benricho.org/Unchiku/Opticalillusion/"
    start_urls = [prefix]
    image_prefix = "https://www.benricho.org/"
    hosts = {"www.benricho.org": 1.0}
    license = "All rights reserved (no licence stated; image origins unclear)"
    license_url = prefix
    flags = {"no_redistribution": "benricho.org (みんなの知識 ちょっと便利帳): " + UNKNOWN}
    max_pages = 20
    max_depth = 1


class IZukan(Gallery):
    source = "izukan"
    prefix = "http://i-zukan.net/ic/"
    start_urls = ["http://i-zukan.net/ic/sh_illusion.htm"]
    image_prefix = "http://i-zukan.net/"
    hosts = {"i-zukan.net": 1.0}
    license = "All rights reserved (no licence stated)"
    license_url = "http://i-zukan.net/ic/sh_illusion.htm"
    flags = {"no_redistribution": "i-zukan.net (インテリア図鑑): " + UNKNOWN}
    max_pages = 5
    max_depth = 0


class Scholarpedia(Gallery):
    source = "scholarpedia"
    prefix = "http://www.scholarpedia.org/article/Visual_illusions"
    start_urls = [prefix]
    image_prefix = "http://www.scholarpedia.org/w/images/"
    hosts = {"www.scholarpedia.org": 1.0}
    license = "CC BY-SA 3.0 (article licence; figures may differ)"
    license_url = "https://creativecommons.org/licenses/by-sa/3.0/"
    flags = {}
    max_pages = 1
    max_depth = 0


class ExploratoriumAnnex(Gallery):
    source = "exploratorium"
    prefix = "https://annex.exploratorium.edu/exhibits/"
    start_urls = [prefix]
    image_prefix = "https://annex.exploratorium.edu/"
    hosts = {"annex.exploratorium.edu": 1.0}
    license = "All rights reserved (Exploratorium)"
    license_url = prefix
    flags = {"no_redistribution": "annex.exploratorium.edu: " + UNKNOWN}
    max_pages = 300
    max_depth = 2


class Wikidebrouillard(Gallery):
    source = "wikidebrouillard"
    prefix = "https://www.wikidebrouillard.org/wiki/Quelques_exemples_d%27illusions_d%27optique"
    start_urls = [prefix]
    image_prefix = "https://www.wikidebrouillard.org/images/"
    hosts = {"www.wikidebrouillard.org": 10.0}           # robots.txt: Crawl-delay: 10
    license = "CC BY-SA 3.0 (wiki content)"
    license_url = "https://creativecommons.org/licenses/by-sa/3.0/"
    flags = {}
    max_pages = 1
    max_depth = 0
