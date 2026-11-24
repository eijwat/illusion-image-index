"""The Illusions Index (University of Glasgow, CSPE) — www.illusionsindex.org.

One illusion per page (/i/<slug>): name, type tags, instructions, effect, illusion credit
(creator, year, first publication) and a discussion. The page states one 'Media Licence' for
its images (e.g. Attribution-NonCommercial-NoDerivs). Images are served from Storyblok's CDN;
'<url>/m/<size>' is a resized rendition, so we strip '/m/...' to get the original file.
Non-visual entries (auditory, tactile...) are kept; their tags say so.
"""
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from .webcrawl import Found, Site

SITE = "www.illusionsindex.org"
CDN = "a.storyblok.com"
CHROME = re.compile(r"logo|ico-|favicon|explore_index|surprise_me|placeholder|/bg_", re.I)

LICENCES = {          # words on the page -> CC short name (the version is not stated on item pages)
    "attribution-noncommercial-noderivs": "CC BY-NC-ND",
    "attribution-noncommercial-sharealike": "CC BY-NC-SA",
    "attribution-noncommercial": "CC BY-NC",
    "attribution-noderivs": "CC BY-ND",
    "attribution-sharealike": "CC BY-SA",
    "attribution": "CC BY",
    "public domain": "Public domain",
}


NO_REDISTRIBUTION = ("The Illusions Index credits this image to a third party without a free licence "
                     "(stated: {}). Treat as all rights reserved.")


def resolve_licence(stated: str) -> tuple[str, bool]:
    """The page's 'Media Licence' words -> (licence short name, redistribution restricted?)."""
    s = (stated or "").strip()
    low = s.lower().replace("_", "-")
    if not s or low == "unknown":
        return "unknown", False
    if m := re.search(r"cc[ -]?(by(?:-(?:nc|nd|sa))*)\s*(\d\.\d)?", low):
        return f"CC {m.group(1).upper()}" + (f" {m.group(2)}" if m.group(2) else ""), False
    if re.search(r"\bcc0\b|public domain", low):
        return "CC0" if "cc0" in low else "Public domain", False
    for words, name in LICENCES.items():
        if low.startswith(words):
            return name, False
    if "copyrighted free use" in low:
        return "Copyrighted free use", False
    if "bsd licen" in low:
        return "BSD", False
    if low in ("creative commons", "share-alike") or "creative commons" in low:
        return f"unknown (stated: {s})", False                   # which CC licence is not said
    return f"All rights reserved (credited: {s[:120]})", True     # a person, company, '©', 'Fair Use'


def _text(html: str | None) -> str | None:
    if not html:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip() or None


def _section(html: str, heading: str) -> str | None:
    m = re.search(rf"<h4>\s*{heading}\s*</h4>(.*?)(?=<h4>|<!--|</div>\s*</div>)", html, re.S | re.I)
    return _text(m.group(1)) if m else None


class IllusionsIndex(Site):
    source = "illusionsindex"
    start_urls = [f"https://{SITE}/i/"]
    hosts = {SITE: 1.0, CDN: 1.0}
    license = "CC BY-NC-SA 4.0 (site); per-page media licence in raw_json"
    license_url = f"https://{SITE}/about"
    flags = {}
    max_pages = 600

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == SITE and p.path.startswith("/i/")

    def follow(self, url: str) -> bool:
        return re.fullmatch(r"/i/[^/]*", urlparse(url).path) is not None

    def extract(self, page_url: str, html: str, tokens: list[tuple]) -> list[Found]:
        if urlparse(page_url).path in ("/i/", "/i"):
            return []
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        name = _text(title.group(1)).split(" - The Illusions Index")[0] if title else None
        licence_words = _section(html, "Media Licence:?") or ""
        licence, restricted = resolve_licence(licence_words)
        credit = _section(html, "ILLUSION CREDIT")
        caption = re.search(r'<p class="fs-5">(.*?)</p>', html, re.S)
        tags = re.findall(r'<a href="/i/#[^"]*"[^>]*>\s*<span[^>]*>(.*?)</span>', html, re.S)
        credit_block = re.search(r"ILLUSION CREDIT(.*?)(?:<footer|$)", html, re.S | re.I)
        discussion = None
        if credit and (m := re.search(r"ILLUSION CREDIT.*?</p>(.*?)(?:<h4>|<footer)", html, re.S | re.I)):
            discussion = _text(m.group(1))
        description = " ".join(x for x in (_text(caption.group(1)) if caption else None,
                                            _section(html, "EFFECT")) if x) or None
        extra = {"tags": [_text(t) for t in tags], "instructions": _section(html, "INSTRUCTIONS"),
                 "effect": _section(html, "EFFECT"), "credit": credit,
                 "discussion": (discussion or "")[:4000] or None,
                 "media_licence": licence_words or None}
        # the publication year: a year near 'published/created/...' that is not part of a life span "(1834 - 1882)"
        credit_text = re.sub(r"\(?\b\d{4}\s*[-–]\s*\d{4}\b\)?", " ", _text(credit_block.group(1)) if credit_block else "")
        year = re.search(r"(?:publish|creat|discover|introduc|describ|report|present|devis|invent)\w*[^.]{0,80}?"
                         r"\b(1[5-9]\d\d|20\d\d)\b", credit_text)

        found, seen = [], set()
        for u in re.findall(r'https://a\.storyblok\.com/f/\d+/[^"\'\s)]+', html):
            original = re.sub(r"/m/.*$", "", u)
            if CHROME.search(original) or original in seen:
                continue
            seen.add(original)
            found.append(Found(original, title=name, description=description,
                               author=credit.split(" was ")[0][:200] if credit else None,
                               date=year.group(1) if year else None,
                               extra=extra | {"license": licence,
                                              "flags": {"no_redistribution": NO_REDISTRIBUTION.format(licence_words)}
                                              if restricted else {}}))
        return found
