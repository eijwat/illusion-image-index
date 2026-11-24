"""SQLite schema and connection.

images  — one row per unique file content (sha256), where it is stored.
items   — one row per (source, source_id): a file as published by a source, with its metadata.
          Several items can point to the same image (same file on several sites).
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    sha256        TEXT PRIMARY KEY,
    ext           TEXT NOT NULL,
    mime          TEXT,
    bytes         INTEGER NOT NULL,
    width         INTEGER,
    height        INTEGER,
    frames        INTEGER,            -- >1 for animated GIF/WebP/PNG
    disk          TEXT NOT NULL,      -- primary disk label (e.g. hdd6)
    mirror_disk   TEXT NOT NULL,      -- mirror disk label (e.g. hdd7)
    rel_path      TEXT NOT NULL,      -- path under <disk>/<store_subdir>/
    mirrored_at   TEXT,               -- set after the mirror copy was verified
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,      -- e.g. 'commons'
    source_id     TEXT NOT NULL,      -- e.g. 'File:Kanizsa triangle.svg'
    page_url      TEXT,
    file_url      TEXT,
    mime          TEXT,
    bytes         INTEGER,
    width         INTEGER,
    height        INTEGER,
    source_sha1   TEXT,               -- checksum published by the source, if any
    title         TEXT,
    description   TEXT,
    author        TEXT,
    credit        TEXT,
    date_created  TEXT,
    license       TEXT,               -- short name, e.g. 'CC BY-SA 4.0'; 'unknown' if not stated
    license_url   TEXT,
    attribution_required INTEGER,
    raw_json      TEXT,               -- full metadata as returned by the source
    status        TEXT NOT NULL DEFAULT 'listed',
        -- listed -> meta_ok -> stored | skipped_mime | too_large | missing | error
        -- zip archives: zip_pending -> expanded; their images are zip_member -> stored
        -- excluded: dropped before download because all its categories are excluded
    error         TEXT,
    image_sha256  TEXT REFERENCES images(sha256),
    listed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    meta_at       TEXT,
    stored_at     TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS items_status ON items(source, status);
CREATE INDEX IF NOT EXISTS items_image ON items(image_sha256);

CREATE TABLE IF NOT EXISTS item_categories (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    category      TEXT NOT NULL,
    PRIMARY KEY (item_id, category)
);

-- Illusion concepts (one per Wikidata item): the name dictionary used to label images.
CREATE TABLE IF NOT EXISTS illusions (
    qid           TEXT PRIMARY KEY,   -- Wikidata ID, e.g. Q1190645
    label_en      TEXT,
    label_ja      TEXT,
    description_en TEXT,
    description_ja TEXT,
    discoverer    TEXT,               -- P61, English label(s)
    inception     TEXT,               -- P571 (year or date)
    commons_category TEXT,            -- P373, without the 'Category:' prefix
    image_file    TEXT,               -- P18, Commons file name
    list_example_file TEXT,           -- example image in the en 'List of optical illusions' row
    extract_en    TEXT,               -- intro of the English Wikipedia article
    extract_ja    TEXT,               -- intro of the Japanese Wikipedia article
    found_via     TEXT,               -- wikidata_class | enwiki_list | both
    entity_json   TEXT,               -- labels/aliases/descriptions in all languages + sitelinks
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which illusion an image item shows (an item can show several).
CREATE TABLE IF NOT EXISTS item_illusions (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    qid           TEXT NOT NULL REFERENCES illusions(qid),
    method        TEXT NOT NULL,      -- p18 | enwiki_list_example | commons_category | commons_subcategory
                                      -- | name_match | appearance
    PRIMARY KEY (item_id, qid)
);
CREATE INDEX IF NOT EXISTS item_illusions_qid ON item_illusions(qid);

-- Judgements and usage restrictions on an item (not an illusion, no AI input...). Items are never deleted for these.
CREATE TABLE IF NOT EXISTS item_flags (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    flag          TEXT NOT NULL,      -- excluded_category | not_illusion | no_redistribution | no_ai_input
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, flag)
);

-- Page crawl state for HTML sites (Kitaoka, Bach, ...).
CREATE TABLE IF NOT EXISTS crawl_pages (
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,      -- normalized
    depth         INTEGER NOT NULL,
    found_on      TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',   -- pending | done | skipped | error
    http_status   INTEGER,
    title         TEXT,
    error         TEXT,
    fetched_at    TEXT,
    PRIMARY KEY (source, url)
);

CREATE TABLE IF NOT EXISTS crawl_categories (
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    parent        TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',   -- pending | done | excluded
    n_files       INTEGER,
    n_subcats     INTEGER,
    done_at       TEXT,
    PRIMARY KEY (source, title)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)   # wait for another crawler's write instead of failing
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con
