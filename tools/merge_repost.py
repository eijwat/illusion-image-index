"""Merge the visual repost check into dbpaper/repost_check.csv (picture, decision, reason, round).

Round 1: work/cache/repost_sheets + repost_answers (2,816 pictures). Round 2: tiles round 1 could not see,
re-rendered on white (repost_sheets2 + repost_answers2) — round 2 replaces round 1 for those pictures.
Strict extra rule (2026-11-24): a 'keep' that rests only on the uploader being the artist (names the checking
agents reported) is turned into 'exclude' unless the Commons file carries a VRT/OTRS permission ticket.
"""
import csv, glob, json, re, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "work" / "cache"
ARTIST_UPLOADERS = re.compile(r"metanamorph|thatpainterguy|john pugh|langlois|limnesh|frantaba|cumini|jean-no|"
                              r"istok|budassi|henrique matos|sokhandan|girometti|christopher willard|vincent tornado",
                              re.I)

def load(sheets, answers, rnd, out):
    man = json.loads((C / sheets / "manifest.json").read_text())
    for f in sorted(glob.glob(str(C / answers / "part_*.json"))):
        for a in json.load(open(f)):
            p = man[str(int(a["sheet"]))][str(int(a["number"]))]
            out[p] = [a["decision"], a.get("reason", "") or "", rnd]

def main():
    out = {}
    load("repost_sheets", "repost_answers", 1, out)
    if (C / "repost_answers2").exists():
        load("repost_sheets2", "repost_answers2", 2, out)
    con = sqlite3.connect(f"file:{ROOT/'work'/'illusions.db'}?mode=ro", uri=True)
    n = 0
    for p, v in out.items():
        if v[0] != "keep":
            continue
        for au, raw in con.execute("""SELECT i.author, i.raw_json FROM items i JOIN picture_files pf
                                      ON pf.sha256 = i.image_sha256 WHERE pf.picture = ? AND i.source = 'commons'""", (p,)):
            m = ARTIST_UPLOADERS.search(au or "")
            if m and not re.search(r"VRT|OTRS", raw or ""):
                v[:] = ["exclude", f"uploader may be the artist ({m.group(0)}) but no VRT permission ticket", v[2]]
                n += 1
                break
    with open(ROOT / "dbpaper" / "repost_check.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["picture", "decision", "reason", "round"])
        for p in sorted(out):
            w.writerow([p, *out[p]])
    from collections import Counter
    print(len(out), Counter(v[0] for v in out.values()), "artist-uploader rule:", n)

main()
