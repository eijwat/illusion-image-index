"""Existing datasets on Hugging Face, read through the public datasets-server API (no download of parquet files).

IllusionVQA (csebuetnlp/illusionVQA-Comprehension): illusion images with a question, options, answer and
an illusion category; useful as labelled reference data for the classification analysis.
"""
import json
import logging
import sqlite3

from .client import PoliteClient
from .config import Settings
from .webcrawl import Found, Site, register

log = logging.getLogger(__name__)

API = "https://datasets-server.huggingface.co"


class IllusionVQA(Site):
    source = "illusionvqa"
    dataset = "csebuetnlp/illusionVQA-Comprehension"
    hosts = {"datasets-server.huggingface.co": 1.0}
    license = "CC BY-NC-SA 4.0 (dataset; some image authors' permission not obtained)"
    license_url = "https://huggingface.co/datasets/csebuetnlp/illusionVQA-Comprehension"
    flags = {"no_redistribution": "IllusionVQA states that permission was not obtained from some image authors; "
                                  "use as reference only. Images are often reposts (see raw_json 'url')."}

    def in_scope(self, url):
        return False

    def extract(self, page_url, html, tokens):
        return []

    def collect(self, s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
        splits = client.get(f"{API}/splits", params={"dataset": self.dataset}).json()["splits"]
        n = 0
        for sp in splits:
            offset = 0
            while True:
                d = client.get(f"{API}/rows", params={"dataset": self.dataset, "config": sp["config"],
                                                      "split": sp["split"], "offset": offset, "length": 100}).json()
                rows = d.get("rows", [])
                for r in rows:
                    row = r["row"]
                    img = (row.get("image") or {}).get("src")
                    if not img:
                        continue
                    options = row.get("options")
                    f = Found(img, title=row.get("question"),
                              description=f"Answer: {row.get('answer')} | Options: {options}",
                              extra={"split": sp["split"], "row": r.get("row_idx"), "id": row.get("id"),
                                     "category": row.get("category"), "answer": row.get("answer"),
                                     "options": options, "original_url": row.get("url"), "source_field": row.get("source")})
                    page = f"https://huggingface.co/datasets/{self.dataset}/viewer/default/{sp['split']}?row={r.get('row_idx')}"
                    if register(con, self, page, f"IllusionVQA {sp['split']} #{row.get('id')}", f, sniff=True):
                        n += 1
                        item_id = con.execute("SELECT id FROM items WHERE source = ? AND source_id = ?",
                                              (self.source, img)).fetchone()[0]
                        con.execute("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                                    (item_id, f"vqa_category:{row.get('category')}"))
                offset += len(rows)
                if not rows or offset >= d.get("num_rows_total", 0):
                    break
            con.commit()
        log.info("illusionvqa: %d images registered", n)


class Viperlib(Site):
    """viperlib.york.ac.uk (vision science image library, offline) recovered from the Internet Archive.

    The optical-illusion-dataset repository (robertmaxwilliams, GitHub) lists 1,454 viperlib entries with
    title, category, description and the 'medium' thumbnail URL; 1,423 of those images are in the Wayback
    Machine (checked 2026-10-06 with one CDX query). We fetch the archived bytes ('id_' URLs) slowly.
    """
    source = "viperlib"
    hosts = {"web.archive.org": 5.0}
    license = "unknown (contributors' works on viperlib; site offline)"
    license_url = "https://github.com/robertmaxwilliams/optical-illusion-dataset"
    flags = {"no_redistribution": "viperlib contributions with no stated licence, recovered from the Internet "
                                  "Archive; treat as all rights reserved."}
    DATA = "work/cache/d2/viperlib_data.json"
    CDX = "work/cache/d2/cdx.txt"

    def in_scope(self, url):
        return False

    def extract(self, page_url, html, tokens):
        return []

    def collect(self, s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
        import re
        archived = {}
        for line in open(self.CDX):
            parts = line.split()
            if len(parts) >= 2:
                key = re.sub(r"^https?://(www\.)?", "", parts[0]).split("?")[0].lower()
                archived[key] = (parts[0], parts[1])
        n = 0
        for x in json.load(open(self.DATA)):
            key = "viperlib.york.ac.uk" + x["url"].split("?")[0].lower()
            if key not in archived:
                continue
            original, ts = archived[key]
            wayback = f"https://web.archive.org/web/{ts}id_/{original}"
            page = "http://viperlib.york.ac.uk" + x["page_url"]
            f = Found(wayback, title=x.get("title"), description=x.get("description"),
                      extra={"category": x.get("category"), "viperlib_page": page, "original_url": original,
                             "archived_at": ts, "recovered_from": "Internet Archive"})
            if register(con, self, page, x.get("title"), f, sniff=True):
                n += 1
                item_id = con.execute("SELECT id FROM items WHERE source = ? AND source_id = ?",
                                      (self.source, wayback)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO item_categories (item_id, category) VALUES (?, ?)",
                            (item_id, f"viperlib:{x.get('category')}"))
        con.commit()
        log.info("viperlib: %d archived images registered", n)
