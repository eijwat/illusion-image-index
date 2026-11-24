"""Build the image zips for Zenodo from an exported data dir (export.py) and the HDD image store.

    python3 dbpaper/zips.py <data dir> <out dir>

images-free.zip  files whose zip == 'free' (public domain, CC0, CC BY, CC BY-SA, FAL ...)
images-nc.zip    files whose zip == 'nc'   (CC with NC and/or ND)
Inside: <label>/<sha256>.<ext> and ATTRIBUTION.csv (one row per licensed source record of each file).
Deterministic: sorted entries, fixed timestamps; images stored, SVG deflated. Each file is checked against its
SHA-256 while it is written.
"""
import csv
import hashlib
import io
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from illusions import config, storage  # noqa: E402
import sqlite3  # noqa: E402

DT = (2026, 11, 24, 0, 0, 0)
README = {
    "free": "Images that their source states are public domain or under a licence allowing redistribution and "
            "commercial use (CC0, CC BY, CC BY-SA, FAL, ...). Each file keeps its own licence: see ATTRIBUTION.csv "
            "and give the attribution it asks for. Share-alike licences (BY-SA) apply to adaptations.\n",
    "nc": "Images under Creative Commons licences with NonCommercial and/or NoDerivatives terms. Each file keeps "
          "its own licence: see ATTRIBUTION.csv. No commercial use where NC applies; no modified versions where "
          "ND applies.\n",
}


def main():
    data, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    s = config.load()
    con = sqlite3.connect(f"file:{ROOT / 'work' / 'illusions.db'}?mode=ro", uri=True)
    loc = {sha: (disk, rel) for sha, disk, rel in con.execute("SELECT sha256, disk, rel_path FROM images")}
    label = {r["picture"]: r["label"] for r in csv.DictReader(open(data / "pictures.csv", encoding="utf-8"))}
    files = [r for r in csv.DictReader(open(data / "files.csv", encoding="utf-8")) if r["zip"]]
    recs = defaultdict(list)
    for r in csv.DictReader(open(data / "records.csv", encoding="utf-8")):
        recs[r["sha256"]].append(r)
    for kind in ("free", "nc"):
        chosen = sorted((f for f in files if f["zip"] == kind), key=lambda f: (label[f["picture"]], f["sha256"]))
        path = out / f"images-{kind}.zip"
        att = io.StringIO()
        w = csv.writer(att)
        w.writerow(["path", "sha256", "picture", "label", "source", "page_url", "title", "author", "credit",
                    "licence", "licence_url"])
        total = 0
        with zipfile.ZipFile(path, "w") as z:
            for f in chosen:
                sha = f["sha256"]
                name = f"{label[f['picture']]}/{sha}.{f['ext']}"
                disk, rel = loc[sha]
                src = storage.disk_root(s, disk) / rel
                b = src.read_bytes()
                if hashlib.sha256(b).hexdigest() != sha:
                    raise SystemExit(f"hash mismatch: {src}")
                zi = zipfile.ZipInfo(name, DT)
                zi.compress_type = zipfile.ZIP_DEFLATED if f["ext"] == "svg" else zipfile.ZIP_STORED
                zi.external_attr = 0o644 << 16
                z.writestr(zi, b)
                total += len(b)
                for r in recs[sha]:
                    if r["licence_class"] == kind:
                        w.writerow([name, sha, f["picture"], label[f["picture"]], r["source"], r["page_url"],
                                    r["title"], r["author"], r["credit"], r["licence"], r["licence_url"]])
            for nm, txt in (("ATTRIBUTION.csv", att.getvalue()), ("README.txt", README[kind])):
                zi = zipfile.ZipInfo(nm, DT)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                z.writestr(zi, txt.encode("utf-8"))
        print(f"{path.name}: {len(chosen)} files, {total / 1e9:.2f} GB of images, zip {path.stat().st_size / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
