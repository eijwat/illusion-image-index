"""Works: versions of one work grouped under a representative picture (2026-10-09, user decision).

Three levels of "the same image":
  1. the same image at another size or format  -> already one picture (pictures / picture_files; rep = largest file)
  2. another version of the same work (colour variant, crop, the same drawing with small changes, a panel that
     contains it) -> grouped here. Only the representative is used; the others stay in the DB, linked to it.
  3. the same illusion drawn by someone else     -> separate works (kept).
Representative = the version that appears most often (pages it is on, files it has), then the highest resolution.
Groups come from visual checks by Claude (method says which); nothing is deleted, and a group can be undone.

picture_tags holds extra marks per picture, e.g. 'demo' = an experiment / hands-on demonstration (pictures of
the set-up or instructions) rather than a picture that shows the illusion by itself.
"""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    picture   TEXT PRIMARY KEY,
    work      TEXT NOT NULL,            -- picture id of the representative
    is_rep    INTEGER NOT NULL,
    method    TEXT NOT NULL,            -- who grouped it, e.g. 'claude-session-kodaka', 'claude-opus-5-5/works-1'
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS picture_tags (
    picture   TEXT NOT NULL,
    tag       TEXT NOT NULL,            -- demo
    reason    TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, tag)
);
"""


def _score(con: sqlite3.Connection, picture: str) -> tuple:
    pages = con.execute("""SELECT count(DISTINCT it.page_url) FROM picture_files f JOIN items it ON it.image_sha256 = f.sha256
                           WHERE f.picture = ?""", (picture,)).fetchone()[0]
    n_files, w, h = con.execute("""SELECT p.n_files, i.width, i.height FROM pictures p JOIN images i ON i.sha256 = p.rep_sha
                                    WHERE p.picture = ?""", (picture,)).fetchone()
    return (pages, n_files or 1, (w or 0) * (h or 0), picture)


def group(con: sqlite3.Connection, pictures: list[str], method: str) -> str:
    """Put pictures (and any works they already belong to) into one work; returns the representative."""
    con.executescript(SCHEMA)
    members = set(pictures)
    for p in pictures:
        r = con.execute("SELECT work FROM works WHERE picture = ?", (p,)).fetchone()
        if r:
            members |= {q for (q,) in con.execute("SELECT picture FROM works WHERE work = ?", (r[0],))}
    rep = max(members, key=lambda p: _score(con, p))
    con.executemany("INSERT OR REPLACE INTO works (picture, work, is_rep, method, at) VALUES (?, ?, ?, ?, datetime('now'))",
                    [(p, rep, int(p == rep), method) for p in members])
    return rep


def tag(con: sqlite3.Connection, pictures: list[str], name: str, reason: str) -> None:
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO picture_tags (picture, tag, reason) VALUES (?, ?, ?)",
                    [(p, name, reason) for p in pictures])


def summary(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    n, w = con.execute("SELECT count(*), count(DISTINCT work) FROM works").fetchone()
    print(f"works: {w} groups covering {n} pictures; {n - w} non-representative versions hidden")
    print("tags:", con.execute("SELECT tag, count(*) FROM picture_tags GROUP BY tag").fetchall())
