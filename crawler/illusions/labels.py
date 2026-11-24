"""Final per-picture label from Claude's blind judgements, and the size cut (2026-10-08, user decisions).

Label: v3 ('claude-opus-5-5/blind-v3', the re-judge with pareidolia / double images / hidden pictures as trick art)
overrides v2. 'trick' = not_illusion tagged trick. The table is derived: `labels` rebuilds it from machine_reviews and
images every run, so nothing here is lost or final.
small = the largest file of the picture fits inside 320x240 in either orientation (SVG never small). Small
pictures stay in the DB and keep their label; they are only left out of the classification.
"""
import sqlite3

MODELS = ("claude-opus-5-5/blind-v2-pilot", "claude-opus-5-5/blind-v2", "claude-opus-5-5/blind-v3")   # later wins
SMALL = (320, 240)

SCHEMA = """
CREATE TABLE IF NOT EXISTS picture_labels (
    picture   TEXT PRIMARY KEY,
    label     TEXT NOT NULL,           -- illusion | not_illusion | trick | unsure
    model     TEXT NOT NULL,           -- which judgement the label came from
    small     INTEGER NOT NULL         -- 1 = fits inside 320x240 (left out of the classification)
);
"""


def is_small(width, height, mime) -> bool:
    if mime == "image/svg+xml" or not width or not height:
        return False
    return max(width, height) < SMALL[0] and min(width, height) < SMALL[1]


def build(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    rank = {m: i for i, m in enumerate(MODELS)}
    best = {}
    for p, m, d, tag in con.execute(f"SELECT picture, model, decision, tag FROM machine_reviews "
                                    f"WHERE model IN ({','.join('?' * len(MODELS))})", MODELS):
        if p not in best or rank[m] > rank[best[p][0]]:
            best[p] = (m, "trick" if tag == "trick" else d)
    size = {p: is_small(w, h, mime) for p, w, h, mime in con.execute(
        "SELECT p.picture, i.width, i.height, i.mime FROM pictures p JOIN images i ON i.sha256 = p.rep_sha")}
    con.execute("DELETE FROM picture_labels")
    con.executemany("INSERT INTO picture_labels VALUES (?, ?, ?, ?)",
                    [(p, lab, m, int(size.get(p, False))) for p, (m, lab) in best.items()])
    con.commit()
    for lab, n, s in con.execute("SELECT label, count(*), sum(small) FROM picture_labels GROUP BY label ORDER BY 2 DESC"):
        print(f"  {lab:13} {n:6}  small {s:5}  kept {n - s:6}")
