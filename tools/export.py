"""Export the public tables of the illusion image index from work/illusions.db (read-only).

Run from the project root:  python3 dbpaper/export.py [out_dir]
Default out_dir: release/illusion-image-index/data

What is left out on purpose (dbpaper/plan.md): source description texts and raw_json (copyrighted text),
Wikipedia extracts (CC BY-SA text), the first-round human reviews (`reviews`, `taxonomy_reviews`),
image files themselves (zips are built separately, see zips.py).
"""
import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "work" / "illusions.db"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "release" / "illusion-image-index" / "data"

# Sources that publish their own works under the stated licence (the author or the publisher itself), so a
# licensed copy there is trusted even if the same picture also appears on a no-redistribution site.
OWN_LICENCE_SOURCES = {"visiome", "joi", "gutenberg"}

CATS = {"G": ("geometric", "幾何学的錯視"), "L": ("lightness and contrast", "明るさ・コントラスト"),
        "C": ("colour", "色"), "M": ("motion", "動き"), "D": ("depth and 3-D shape", "奥行き・立体・空間"),
        "K": ("completion and illusory contours", "補完・主観的輪郭"), "A": ("ambiguous figures", "多義図形"),
        "I": ("impossible figures", "不可能図形"), "F": ("face and object recognition", "顔・物体の認識"),
        "R": ("afterimage, adaptation and fading", "残像・順応・消失"), "X": ("multimodal", "マルチモーダル"),
        "T": ("trick art (not an illusion)", "だまし絵・トリックアート")}
LABEL_MODELS = ("claude-opus-5-5/blind-v2-pilot", "claude-opus-5-5/blind-v2", "claude-opus-5-5/blind-v3")
TAX_MODELS = ("claude-opus-5-5/tax-blind-1", "claude-opus-5-5/tax-blind-2")   # later wins


def licence(raw):
    """-> (normalised name, class) with class free | nc | none."""
    if raw is None:
        return "unknown", "none"
    s = raw.strip()
    L = s.lower()
    if L in ("public domain", "no restrictions"):
        return ("PD" if L == "public domain" else "no known restrictions"), "free"
    if L == "cc0":
        return "CC0-1.0", "free"
    if L in ("copyrighted free use", "attribution"):
        return s, "free"
    if L == "fal":
        return "FAL-1.3", "free"
    m = re.fullmatch(r"cc ((?:by|sa|nc|nd)(?:-(?:by|sa|nc|nd))*)(?: (\d\.\d))?(?: (?:([a-z]{2,3})|\(wiki content\)))?", L)
    if m:
        parts, ver, port = m.group(1), m.group(2), m.group(3)
        name = "CC-" + parts.upper() + (f"-{ver}" if ver else "") + (f"-{port.upper()}" if port else "")
        tok = parts.split("-")
        return name, ("nc" if ("nc" in tok or "nd" in tok) else "free")
    # GPL / GFDL / BSD / AGPL (software-style licences, 19 files), unknown, all rights reserved: not bundled
    return s, "none"


OWN_WORK = re.compile(r"own work|selbst|eigenes werk|travail personnel|自作|trabajo propio|opera propria|"
                      r"trabalho próprio|eigen werk", re.I)
# Artists / designers of protected illusion artworks; a licensed file that names one is left out of the zips
# unless it comes from the artist's own licensed release (OWN_LICENCE_SOURCES).
ARTISTS = re.compile(r"kitaoka|北岡|rotating snake|蛇の回転|escher|ocampo|gonsalves|del[- ]?prete|dal[ií]\b|"
                     r"magritte|vasarely|bridget riley|sugihara|杉原|[ōo]uchi|大内|shepard|adelson|checker ?shadow|"
                     r"pinna|lotto|lilac chaser|hinton|bach\b|anstis|akiyoshi|kanizsa|fraser spiral|"
                     r"ames room|julian beever|kurt wenner|edgar m[üu]ller|felice varini|patrick hughes|"
                     r"istvan orosz|orosz|oscar reutersv[äa]rd|reutersv[äa]rd|sandro", re.I)


def near_matches(cands, nr_files, hashes, pf, work, maxd=10):
    """Licensed files whose dHash is within maxd bits of a no-redistribution file of another picture group and
    whose aHash is also within maxd bits. Pigeonhole index: 11 chunks of 6 bits (64 = 10*6 + 4)."""
    def bits(h):
        try:
            return int(h, 16)
        except (TypeError, ValueError):
            return None
    chunks = [(i * 6, 6 if i < 10 else 4) for i in range(11)]
    idx = [defaultdict(list) for _ in chunks]
    nr = []
    for s in sorted(nr_files):
        d, a = (bits(x) for x in hashes.get(s, (None, None)))
        if d is None or a is None:
            continue
        nr.append((s, d, a))
        for k, (o, n) in enumerate(chunks):
            idx[k][(d >> o) & ((1 << n) - 1)].append(len(nr) - 1)
    g = lambda s: work.get(pf.get(s), (pf.get(s), 1))[0]
    src_of = {}
    out = {}
    for s in cands:
        d, a = (bits(x) for x in hashes.get(s, (None, None)))
        if d is None or a is None or (d == 0 and a == 0):
            continue
        seen, best = set(), None                     # nearest match (then lowest sha): the same every run
        for k, (o, n) in enumerate(chunks):
            for j in idx[k].get((d >> o) & ((1 << n) - 1), ()):
                if j in seen:
                    continue
                seen.add(j)
                t, dd, aa = nr[j]
                dist = ((d ^ dd).bit_count() + (a ^ aa).bit_count(), t)
                if g(t) != g(s) and (d ^ dd).bit_count() <= maxd and (a ^ aa).bit_count() <= maxd \
                        and (best is None or dist < best):
                    best = dist
        if best:
            out[s] = best[1]
    return out


def clean_title(t, source, source_id):
    """Commons titles sometimes carry Wikidata label dumps ('label QS:Les,...'); fall back to the file name."""
    t = (t or "").strip()
    if source == "commons" and ("label QS:" in t or len(t) > 300):
        t = re.sub(r"^File:", "", source_id)
    return t[:500]


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    OUT.mkdir(parents=True, exist_ok=True)

    pf = dict(q("SELECT sha256, picture FROM picture_files"))
    work = {p: (w, r) for p, w, r in q("SELECT picture, work, is_rep FROM works")}
    flags = defaultdict(set)
    for iid, fl in q("SELECT item_id, flag FROM item_flags"):
        flags[iid].add(fl)
    items = q("""SELECT id, source, source_id, page_url, file_url, title, author, credit, date_created, license,
                        license_url, image_sha256 FROM items WHERE status = 'stored' AND image_sha256 IS NOT NULL
                 ORDER BY id""")

    # --- licence class per file, with the conflict rule
    by_file = defaultdict(list)
    for it in items:
        name, cls = licence(it[9])
        if "no_redistribution" in flags[it[0]]:
            cls = "none"
        by_file[it[11]].append((it[1], cls, it[0]))
    group_nr = defaultdict(set)          # picture group (work if grouped) -> no-redistribution sources
    group = lambda sha: work.get(pf.get(sha), (pf.get(sha), 1))[0]
    for it in items:
        if "no_redistribution" in flags[it[0]]:
            group_nr[group(it[11])].add(it[1])
    # Strict screen for possible reposts (user, 2026-11-24: leave out anything with even a slight doubt).
    # A licensed file goes into a zip only if it passes every rule below; otherwise zip_note says why not.
    lab_of = dict(q("SELECT picture, label FROM picture_labels"))
    text_of = defaultdict(str)                       # picture -> Claude's notes and recognised name
    for p, note in q("SELECT picture, note FROM machine_reviews WHERE note IS NOT NULL"):
        text_of[p] += " " + note
    for p, name, note in q("SELECT picture, name, note FROM taxonomy_machine"):
        text_of[p] += f" {name or ''} {note or ''}"
    item_text = defaultdict(str)
    for it in items:
        item_text[it[11]] += f" {it[5] or ''} {it[6] or ''} {it[7] or ''} {it[2]}"
    credit = defaultdict(list)
    for it in items:
        credit[it[11]].append((it[1], (it[7] or "").strip(), licence(it[9])[0]))
    hashes = {s: (d, a) for s, d, a in q("SELECT sha256, dhash, ahash FROM image_hashes")}
    nr_files = {it[11] for it in items if "no_redistribution" in flags[it[0]]}
    near_nr = near_matches([s for s, lst in by_file.items() if any(c != "none" for _, c, _ in lst)],
                           nr_files, hashes, pf, work)
    visual = {}                                      # visual repost check (merge_repost.py); absent = not checked
    vfile = Path(__file__).resolve().parent / "repost_check.csv"
    if vfile.exists():
        visual = {r["picture"]: r for r in csv.DictReader(open(vfile, encoding="utf-8"))}
    file_zip, file_note = {}, {}
    for sha, lst in by_file.items():
        best = "free" if any(c == "free" for _, c, _ in lst) else ("nc" if any(c == "nc" for _, c, _ in lst) else "")
        if best:
            lic_srcs = {s for s, c, _ in lst if c == best}
            p = pf.get(sha)
            lab = lab_of.get(p, "")
            why = ""
            if group_nr.get(group(sha)) and not (lic_srcs & OWN_LICENCE_SOURCES):
                why = "licensed copy of a picture also on " + "/".join(sorted(group_nr[group(sha)]))
            elif sha in near_nr and not (lic_srcs & OWN_LICENCE_SOURCES):
                why = "looks like a picture on " + "/".join(sorted({x for x, c, _ in by_file[near_nr[sha]] if c == "none"})) + " (perceptual hash)"
            elif lic_srcs == {"openverse"} and lab != "not_illusion":
                why = "Flickr upload of an illusion/trick picture; uploader may not be the author"
            elif "commons" in lic_srcs and not lic_srcs & (OWN_LICENCE_SOURCES | {"openverse"}) and not any(
                    s == "commons" and (OWN_WORK.search(cr) or lic == "PD") for s, cr, lic in credit[sha]):
                why = "Commons file not marked as own work (and not public domain)"
            elif not lic_srcs & OWN_LICENCE_SOURCES and ARTISTS.search(item_text[sha] + text_of.get(p, "")):
                why = "names an artist or a protected artwork: " + ARTISTS.search(item_text[sha] + text_of.get(p, "")).group(0)
            elif lab == "not_illusion":
                why = "labelled not an illusion: metadata and link only (user decision 2026-11-24)"
            elif lab in ("illusion", "unsure", "trick") and visual.get(p, {}).get("decision") != "keep":
                why = ("visual check: " + visual[p]["reason"]) if p in visual else "not visually checked"
            if why:
                file_note[sha] = why
                best = ""
        file_zip[sha] = best

    # --- files.csv
    hashes = {s: (d, a) for s, d, a in q("SELECT sha256, dhash, ahash FROM image_hashes")}
    files = q("SELECT sha256, ext, mime, bytes, width, height, frames FROM images ORDER BY sha256")
    with open(OUT / "files.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sha256", "picture", "ext", "mime", "bytes", "width", "height", "frames", "dhash", "ahash",
                    "zip", "zip_note"])
        for sha, ext, mime, b, wd, ht, fr in files:
            if sha not in pf:
                continue
            d, a = hashes.get(sha, ("", ""))
            w.writerow([sha, pf[sha], ext, mime, b, wd, ht, fr, d or "", a or "", file_zip.get(sha, ""),
                        file_note.get(sha, "")])

    # --- records.csv
    with open(OUT / "records.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["record", "source", "source_id", "page_url", "file_url", "sha256", "picture", "title",
                    "author", "credit", "date", "licence_stated", "licence", "licence_class", "licence_url", "flags"])
        for (iid, src, sid, page, furl, title, author, credit, date, lic, lurl, sha) in items:
            name, cls = licence(lic)
            if "no_redistribution" in flags[iid]:
                cls = "none"
            w.writerow([iid, src, sid, page or "", furl or "", sha, pf.get(sha, ""), clean_title(title, src, sid), author or "",
                        credit or "", date or "", lic or "", name, cls, lurl or "",
                        ";".join(sorted(flags[iid]))])

    # --- pictures.csv
    labels = {p: (lab, m, s) for p, lab, m, s in q("SELECT picture, label, model, small FROM picture_labels")}
    lnote = {}
    rank = {m: i for i, m in enumerate(LABEL_MODELS)}
    for p, m, note in q(f"SELECT picture, model, note FROM machine_reviews WHERE model IN "
                        f"({','.join('?' * len(LABEL_MODELS))})", *LABEL_MODELS):
        if p not in lnote or rank[m] > lnote[p][0]:
            lnote[p] = (rank[m], note or "")
    tax = {}
    trank = {m: i for i, m in enumerate(TAX_MODELS)}
    for p, m, cats, main, name, note in q("SELECT picture, model, cats, main, name, note FROM taxonomy_machine"):
        if p not in tax or trank[m] > tax[p][0]:
            tax[p] = (trank[m], m, json.loads(cats), main or "", name or "", note or "")
    tags = defaultdict(set)
    for p, t in q("SELECT picture, tag FROM picture_tags"):
        tags[p].add(t)
    pic_flags, pic_zip, pic_src = defaultdict(set), defaultdict(set), defaultdict(set)
    for it in items:
        p = pf.get(it[11])
        pic_flags[p] |= flags[it[0]]
        pic_src[p].add(it[1])
        if file_zip.get(it[11]):
            pic_zip[p].add(file_zip[it[11]])
    reps = {p: (r, wd, ht, fr, ext) for p, r, wd, ht, fr, ext in q(
        "SELECT p.picture, p.rep_sha, i.width, i.height, i.frames, i.ext FROM pictures p JOIN images i ON i.sha256 = p.rep_sha")}
    count = Counter()
    with open(OUT / "pictures.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["picture", "title", "label", "label_model", "label_note", "small", "work", "work_rep",
                    "in_classification", "category_main", "categories", "illusion_name", "category_model",
                    "category_note", "demo", "tier", "n_files", "rep_sha256", "width", "height", "frames", "ext",
                    "sources", "zip", "no_ai_input", "flags"])
        for p, tier, n, title in q("SELECT picture, tier, n_files, title FROM pictures ORDER BY picture"):
            lab, lm, small = labels.get(p, ("", "", 0))
            wk, wr = work.get(p, (p, 1))
            t = tax.get(p)
            incl = int(lab == "illusion" and not small and wr == 1)
            r, wd, ht, fr, ext = reps[p]
            z = "free" if "free" in pic_zip[p] else ("nc" if "nc" in pic_zip[p] else "")
            fl = pic_flags[p] - {"no_redistribution", "no_ai_input"}
            w.writerow([p, title or "", lab, lm, lnote.get(p, (0, ""))[1], small, wk, wr, incl,
                        t[3] if t else "", ";".join(t[2]) if t else "", t[4] if t else "", t[1] if t else "",
                        t[5] if t else "", int("demo" in tags[p]), tier, n, r, wd, ht, fr, ext,
                        ";".join(sorted(pic_src[p])), z, int("no_ai_input" in pic_flags[p]), ";".join(sorted(fl))])
            count[(lab, z)] += 1

    # --- concepts (Wikidata facts only, CC0) and record links
    with open(OUT / "concepts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["qid", "label_en", "label_ja", "description_en", "description_ja", "discoverer", "inception",
                    "kind", "core", "n_languages", "commons_category", "found_via"])
        for r in q("""SELECT qid, label_en, label_ja, description_en, description_ja, discoverer, inception, kind,
                             core, n_languages, commons_category, found_via FROM illusions ORDER BY qid"""):
            w.writerow(["" if v is None else v for v in r])
    stored = {it[0] for it in items}
    with open(OUT / "record_concepts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["record", "qid", "method"])
        for r in q("SELECT item_id, qid, method FROM item_illusions ORDER BY item_id, qid"):
            if r[0] in stored:
                w.writerow(r)

    # --- blind human checks (author's own judgements)
    with open(OUT / "check_labels.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # stratum = the v2 decision the sample was drawn from (100 each); claude_v2 also reads v2's trick tag
        w.writerow(["picture", "order", "stratum", "claude_v2", "claude_final", "human"])
        for p, o, m, h in q("""SELECT c.picture, c.ord, c.machine, h.decision FROM v2_check_queue c
                               JOIN v2_human h ON h.picture = c.picture ORDER BY c.ord"""):
            v2 = con.execute("""SELECT decision, tag FROM machine_reviews WHERE picture = ? AND model IN
                      ('claude-opus-5-5/blind-v2', 'claude-opus-5-5/blind-v2-pilot') ORDER BY model DESC""",
                             (p,)).fetchone()
            w.writerow([p, o, m, "trick" if v2[1] == "trick" else v2[0], labels.get(p, ("",))[0], h])
    with open(OUT / "check_categories.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["picture", "order", "claude_main_at_check", "claude_main_final", "claude_categories_final",
                    "human_categories"])
        for p, o, m, h in q("""SELECT c.picture, c.ord, c.machine, h.cats FROM tax2_check_queue c
                               JOIN tax2_human h ON h.picture = c.picture ORDER BY c.ord"""):
            t = tax.get(p)
            w.writerow([p, o, m, t[3] if t else "", ";".join(t[2]) if t else "", ";".join(json.loads(h))])

    with open(OUT / "categories.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "name_en", "name_ja"])
        for k, (en, ja) in CATS.items():
            w.writerow([k, en, ja])

    # --- summary
    print("pictures by label x zip:")
    for k, v in sorted(count.items()):
        print("  ", k, v)
    zf = Counter(file_zip[s] for s in pf if s in file_zip)
    zb = Counter()
    for sha, ext, mime, b, *_ in files:
        if file_zip.get(sha):
            zb[file_zip[sha]] += b
    print("files by zip:", dict(zf), {k: f"{v / 1e9:.2f} GB" for k, v in zb.items()})
    print("conflict-excluded files:", len(file_note))
    for p in sorted(OUT.glob("*.csv")):
        print(f"  {p.name:22} {p.stat().st_size / 1e6:7.1f} MB")


if __name__ == "__main__":
    main()
