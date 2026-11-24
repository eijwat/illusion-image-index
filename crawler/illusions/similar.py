"""Perceptual hashes and near-duplicate matching (Pillow only).

Each raster image gets two 64-bit hashes of its first frame:
  dhash — sign of horizontal brightness gradients on a 9x8 thumbnail
  ahash — pixels above/below the mean on an 8x8 thumbnail
Two images are 'the same picture' when both hashes are within a few bits and the aspect ratios
agree; that survives re-encoding, resizing and format changes. SVG is skipped (no rasterizer).

Uses of the matches:
  - Kitaoka works that also exist on Visiome under a CC license -> flag cc_version_available
  - smaller copies of an image in the same source (thumbnails)    -> flag thumbnail
  - remaining tiny images (<100 px) on HTML sites                 -> flag likely_navigation
"""
import json
import logging
import re
import sqlite3
from collections import defaultdict

from PIL import Image

from . import storage
from .config import Settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_hashes (
    sha256   TEXT PRIMARY KEY REFERENCES images(sha256),
    dhash    TEXT,              -- 16 hex digits; NULL if the image could not be read
    ahash    TEXT,
    csig     TEXT,              -- mean RGB of a 3x3 grid, 27 bytes as hex (hashes ignore colour)
    error    TEXT
);
CREATE TABLE IF NOT EXISTS image_matches (
    sha_a    TEXT NOT NULL,     -- the larger image (by pixel count)
    sha_b    TEXT NOT NULL,
    dist_d   INTEGER NOT NULL,
    dist_a   INTEGER NOT NULL,
    PRIMARY KEY (sha_a, sha_b)
);
"""
MAX_DIST = 6            # bits, for each of the two hashes
MAX_ASPECT_DIFF = 0.08  # relative difference of width/height
MAX_COLOUR_DIFF = 4     # mean absolute difference of the 3x3 RGB grid (0-255); 12 let grey/colour versions match
CC_MAX_FINE_DIST = 6    # 16x16 dHash bits (of 256) required for cc_version_available: precision over recall,
                        # since publishing a CC copy instead of a Kitaoka original relies on it being the same work
THUMBNAIL_RATIO = 0.6   # smaller side <= 60% of the larger image's side
NAV_MAX_SIDE = 100


# ---------------------------------------------------------------- hashing

def _hashes(path) -> tuple[str, str, str]:
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        im.seek(0)
        if im.format == "JPEG":
            im.draft("RGB", (64, 64))                # decode at reduced size: much faster
        rgb = im.convert("RGB")
    g = rgb.convert("L")
    csig = bytes(v for px in rgb.resize((3, 3), Image.Resampling.BOX).getdata() for v in px).hex()
    d = g.resize((9, 8), Image.Resampling.LANCZOS)
    px = list(d.getdata())
    dbits = [px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)]
    a = list(g.resize((8, 8), Image.Resampling.LANCZOS).getdata())
    mean = sum(a) / 64
    abits = [p > mean for p in a]
    to_hex = lambda bits: f"{sum(1 << i for i, b in enumerate(bits) if b):016x}"
    return to_hex(dbits), to_hex(abits), csig


def compute_hashes(s: Settings, con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    if "csig" not in [c[1] for c in con.execute("PRAGMA table_info(image_hashes)")]:
        con.execute("ALTER TABLE image_hashes ADD COLUMN csig TEXT")
    rows = con.execute("SELECT i.sha256, i.disk, i.rel_path FROM images i LEFT JOIN image_hashes h USING (sha256) "
                       "WHERE (h.sha256 IS NULL OR (h.csig IS NULL AND h.error IS NULL)) "
                       "AND i.mime != 'image/svg+xml'").fetchall()
    log.info("hashing %d images", len(rows))
    roots: dict[str, object] = {}
    for n, r in enumerate(rows, 1):
        root = roots.setdefault(r["disk"], storage.disk_root(s, r["disk"]))
        try:
            dh, ah, cs = _hashes(root / r["rel_path"])
            con.execute("INSERT OR REPLACE INTO image_hashes (sha256, dhash, ahash, csig) VALUES (?, ?, ?, ?)",
                        (r["sha256"], dh, ah, cs))
        except Exception as e:
            con.execute("INSERT OR REPLACE INTO image_hashes (sha256, error) VALUES (?, ?)", (r["sha256"], str(e)[:300]))
        if n % 1000 == 0:
            con.commit()
            log.info("hashed %d/%d", n, len(rows))
    con.commit()


# ---------------------------------------------------------------- matching

def find_matches(con: sqlite3.Connection) -> None:
    """All near-duplicate pairs, using 8-bit bands of dhash as buckets (any pair within 7 bits shares a band)."""
    con.executescript(SCHEMA)
    rows = con.execute("SELECT h.sha256, h.dhash, h.ahash, h.csig, i.width, i.height FROM image_hashes h "
                       "JOIN images i USING (sha256) WHERE h.dhash IS NOT NULL AND h.csig IS NOT NULL").fetchall()
    info = {r["sha256"]: (int(r["dhash"], 16), int(r["ahash"], 16), r["width"] or 1, r["height"] or 1)
            for r in rows}
    colour = {r["sha256"]: bytes.fromhex(r["csig"]) for r in rows}
    # Near-uniform images (blank, flat colour) hash to almost nothing; they would match everything.
    usable = {k: v for k, v in info.items() if 6 <= bin(v[0]).count("1") <= 58}
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for k, (dh, _, _, _) in usable.items():
        for band in range(8):
            buckets[(band, (dh >> (8 * band)) & 0xFF)].append(k)

    con.execute("DELETE FROM image_matches")
    pairs = set()
    for members in buckets.values():
        if len(members) > 2000:
            continue                                   # degenerate band value; other bands still catch real pairs
        for i, a in enumerate(members):
            da, aa, wa, ha = usable[a]
            for b in members[i + 1:]:
                if (a, b) in pairs or (b, a) in pairs:
                    continue
                db, ab, wb, hb = usable[b]
                dd, ad = bin(da ^ db).count("1"), bin(aa ^ ab).count("1")
                if dd > MAX_DIST or ad > MAX_DIST:
                    continue
                ra, rb = wa / ha, wb / hb
                if abs(ra - rb) / max(ra, rb) > MAX_ASPECT_DIFF:
                    continue
                ca, cb = colour[a], colour[b]
                if sum(abs(x - y) for x, y in zip(ca, cb)) / len(ca) > MAX_COLOUR_DIFF:
                    continue                           # same layout, different colours: another work
                big, small = (a, b) if wa * ha >= wb * hb else (b, a)
                pairs.add((big, small))
                con.execute("INSERT INTO image_matches (sha_a, sha_b, dist_d, dist_a) VALUES (?, ?, ?, ?)",
                            (big, small, dd, ad))
    con.commit()
    log.info("near-duplicate pairs: %d (among %d hashed images, %d usable)", len(pairs), len(info), len(usable))


# ---------------------------------------------------------------- flags

def _fine_dhash(path, n: int = 16) -> int:
    with Image.open(path) as im:
        im.seek(0)
        g = im.convert("L").resize((n + 1, n), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    return sum(1 << (r * n + c) for r in range(n) for c in range(n) if px[r * (n + 1) + c] > px[r * (n + 1) + c + 1])


def apply_flags(s: Settings, con: sqlite3.Connection) -> None:
    """(Re)derive the similarity flags. Only flags this module owns are replaced."""
    paths = {r[0]: (r[1], r[2]) for r in con.execute("SELECT sha256, disk, rel_path FROM images")}
    fine_cache: dict[str, int] = {}

    def fine(sha: str) -> int:
        if sha not in fine_cache:
            disk, rel = paths[sha]
            fine_cache[sha] = _fine_dhash(storage.disk_root(s, disk) / rel)
        return fine_cache[sha]
    con.execute("DELETE FROM item_flags WHERE flag IN ('cc_version_available', 'thumbnail', 'likely_navigation')")
    items_by_sha: dict[str, list] = defaultdict(list)
    for r in con.execute("SELECT id, source, source_id, license, title, image_sha256 FROM items WHERE status = 'stored'"):
        items_by_sha[r["image_sha256"]].append(r)
    size = {r[0]: (r[1] or 0, r[2] or 0) for r in con.execute("SELECT sha256, width, height FROM images")}
    flags: list[tuple[int, str, str]] = []

    def cc_pairs(sha_x: str, sha_y: str) -> None:
        if not any(k["source"] == "kitaoka" for k in items_by_sha[sha_x]) or \
                not any(v["source"] == "visiome" for v in items_by_sha[sha_y]):
            return
        if sha_x != sha_y and bin(fine(sha_x) ^ fine(sha_y)).count("1") > CC_MAX_FINE_DIST:
            return                                           # same layout, but not the same rendering
        for k in items_by_sha[sha_x]:
            for v in items_by_sha[sha_y]:
                if k["source"] == "kitaoka" and v["source"] == "visiome" and (v["license"] or "").startswith("CC"):
                    flags.append((k["id"], "cc_version_available", json.dumps(
                        {"visiome_item": v["id"], "visiome_source_id": v["source_id"],
                         "license": v["license"], "title": v["title"]}, ensure_ascii=False)))

    for sha in items_by_sha:                                 # identical files
        cc_pairs(sha, sha)
    for big, small in con.execute("SELECT sha_a, sha_b FROM image_matches").fetchall():
        cc_pairs(big, small)
        cc_pairs(small, big)
        (wa, ha), (wb, hb) = size[big], size[small]
        if max(wb, hb) <= THUMBNAIL_RATIO * max(wa, ha):     # thumbnails within one source
            big_sources = {i["source"]: i["source_id"] for i in items_by_sha[big]}
            for i in items_by_sha[small]:
                if i["source"] in big_sources:
                    flags.append((i["id"], "thumbnail",
                                  f"smaller copy of {big_sources[i['source']]} ({wb}x{hb} vs {wa}x{ha})"))
    thumbs = {f[0] for f in flags if f[1] == "thumbnail"}
    for sha, its in items_by_sha.items():                   # tiny images that are not thumbnails of a work
        w, h = size.get(sha, (0, 0))
        if max(w, h) < NAV_MAX_SIDE:
            for i in its:
                if i["source"] in ("kitaoka", "bach") and i["id"] not in thumbs:
                    flags.append((i["id"], "likely_navigation", f"{w}x{h} px, no larger copy found"))
    con.executemany("INSERT OR IGNORE INTO item_flags (item_id, flag, reason) VALUES (?, ?, ?)", flags)
    con.commit()
    counts = defaultdict(int)
    for item_id, flag, _ in set((f[0], f[1], "") for f in flags):
        counts[flag] += 1
    log.info("flags: %s", dict(counts))
