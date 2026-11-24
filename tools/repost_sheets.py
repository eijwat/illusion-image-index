"""Contact sheets for the visual repost check (2026-11-24, user decision).

Candidates: pictures labelled illusion / unsure / trick that still have a file in a zip after the automatic
screen in export.py (read from the exported pictures.csv). Each sheet has 6 tiles (3x2, 520 px); the text of the
source records (source, title, author, credit, licence) goes into sheet_NNNN.txt next to it, because the check
is about provenance, not about the illusion.

    python3 dbpaper/repost_sheets.py <exported data dir> <out dir>
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from illusions import config  # noqa: E402
from illusions.machine_review import sheets_v2  # noqa: E402


def main():
    data, out = Path(sys.argv[1]), Path(sys.argv[2])
    pics = [r["picture"] for r in csv.DictReader(open(data / "pictures.csv", encoding="utf-8"))
            if r["zip"] and r["label"] in ("illusion", "unsure", "trick")]
    pics.sort()
    recs = defaultdict(list)
    for r in csv.DictReader(open(data / "records.csv", encoding="utf-8")):
        recs[r["picture"]].append(r)
    s = config.load()
    con = sqlite3.connect(f"file:{ROOT / 'work' / 'illusions.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    n = sheets_v2(s, con, pics, out, cols=3, rows_=2, tile=520, shuffle=False)
    manifest = json.loads((out / "manifest.json").read_text())
    for sheet, m in manifest.items():
        lines = []
        for num, p in sorted(m.items(), key=lambda x: int(x[0])):
            lines.append(f"#{num}")
            seen = set()
            for r in recs[p]:
                key = (r["source"], r["title"], r["author"], r["credit"], r["licence"])
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"  source={r['source']} | title={r['title'][:200]} | author={r['author'][:120]} | "
                             f"credit={r['credit'][:200]} | licence={r['licence']} | page={r['page_url'][:150]}")
        (out / f"sheet_{int(sheet):04d}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(n, "sheets,", len(pics), "pictures")


if __name__ == "__main__":
    main()
