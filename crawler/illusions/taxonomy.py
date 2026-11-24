"""Illusion categories (sources/taxonomy/crosswalk.md): initial labels and the human category review.

Super-categories: illusion (the answer lies in the eye or brain) / trick (trick art: the answer is plain, it is
about how the picture is made) / out (not in scope). Under "illusion", categories by what looks different;
a picture may carry several.

prepare()  derives initial labels (taxonomy_auto) from the illusion-name dictionary (sources/taxonomy/concepts.tsv)
           and the sources' own classifications (source_map.tsv + schemes/*.json), and builds the review queues
           (taxonomy_queue) from pictures a person judged to be an illusion (or unsure).
Pages      /tax (queue list) and /tax/grid in the review app; decisions go to taxonomy_reviews.
"""
import json
import logging
import sqlite3
from collections import defaultdict
from html import escape
from urllib.parse import quote

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

DIR = PROJECT_ROOT / "sources" / "taxonomy"
CATS = {"G": "幾何学的錯視", "L": "明るさ・コントラスト", "C": "色", "M": "動き", "D": "奥行き・立体・空間",
        "K": "補完・主観的輪郭", "A": "多義図形", "I": "不可能図形", "F": "顔・物体の認識", "R": "残像・順応・消失",
        "X": "マルチモーダル", "T": "だまし絵・トリックアート"}
OUT = "out"                                    # 対象外 (shown as its own chip)
PAGE_SIZE = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS taxonomy_auto (
    picture   TEXT NOT NULL,
    super     TEXT NOT NULL,          -- illusion | trick | out | broad
    cat       TEXT NOT NULL,          -- one of CATS, or '' (super only)
    origin    TEXT NOT NULL,          -- concept | source
    detail    TEXT NOT NULL,          -- qid, or scheme:rule
    PRIMARY KEY (picture, cat, origin, detail)
);
CREATE TABLE IF NOT EXISTS taxonomy_queue (
    queue     TEXT NOT NULL,
    picture   TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    PRIMARY KEY (queue, picture)
);
CREATE TABLE IF NOT EXISTS taxonomy_reviews (
    picture   TEXT NOT NULL,
    reviewer  TEXT NOT NULL,
    cats      TEXT NOT NULL,          -- JSON list of CATS keys, plus 'out' / 'unsure'
    queue     TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, reviewer)
);
"""


def _tsv(name: str) -> list[list[str]]:
    rows = []
    for line in (DIR / name).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            rows.append((line.split("\t") + [""] * 5)[:5])
    return rows


def _norm(url: str) -> str:
    return url.replace("http://", "https://").replace("/~akitaoka/", "/akitaoka/").rstrip("/")


def _source_rules() -> list[tuple[str, str, str, list[str], set[str] | str]]:
    """(scheme, rule, super, cats, page urls or a url prefix)"""
    schemes = {}
    out = []
    for scheme, rule, sup, cats, _ in _tsv("source_map.tsv"):
        if scheme not in schemes:
            schemes[scheme] = json.loads((DIR / "schemes" / f"{scheme}.json").read_text(encoding="utf-8"))
        cat_list = [c for c in cats.split(",") if c]
        if rule.startswith("prefix:"):                    # Bach: https://michaelbach.de/ot/<prefix>-<name>/
            out.append((scheme, rule, sup, cat_list, f"https://michaelbach.de/ot/{rule[7:]}-"))
            continue
        cat = next(c for c in schemes[scheme]["categories"] if c["id"] == rule)
        urls = set()
        for m in cat.get("members") or []:
            for u in [m.get("url")] + (m.get("urls") or []):
                if u:
                    urls.add(_norm(u))
        urls -= {_norm(u) for u in (cat.get("url"), cat.get("url_en")) if u}   # the index page itself lists every kind
        out.append((scheme, rule, sup, cat_list, urls))
    return out


def prepare(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    pic_of = dict(con.execute("SELECT sha256, picture FROM picture_files"))
    auto = set()

    concepts = {qid: (sup, [c for c in cats.split(",") if c]) for qid, sup, cats, *_ in _tsv("concepts.tsv")}
    for qid, sha in con.execute("SELECT DISTINCT ii.qid, it.image_sha256 FROM item_illusions ii "
                                "JOIN items it ON it.id = ii.item_id AND it.status = 'stored'"):
        pic = pic_of.get(sha)
        if pic and qid in concepts:
            sup, cats = concepts[qid]
            for c in cats or [""]:
                auto.add((pic, sup, c, "concept", qid))

    rules = _source_rules()
    for sha, page in con.execute("SELECT image_sha256, page_url FROM items WHERE status = 'stored' AND page_url IS NOT NULL"):
        pic = pic_of.get(sha)
        if not pic:
            continue
        page = _norm(page)
        for scheme, rule, sup, cats, match in rules:
            hit = page.startswith(match.rstrip("/")) if isinstance(match, str) else \
                  any(page == u or page.startswith(u + "/") for u in match) if scheme == "ntt" else page in match
            if hit and sup:
                for c in cats or [""]:
                    auto.add((pic, sup, c, "source", f"{scheme}:{rule}"))

    con.execute("DELETE FROM taxonomy_auto")
    con.executemany("INSERT INTO taxonomy_auto VALUES (?, ?, ?, ?, ?)", sorted(auto))

    # review queues: pictures a person judged an illusion (or was unsure about), grouped by their first initial category
    order = list(CATS)
    first = {}
    for pic, cat in con.execute("SELECT picture, cat FROM taxonomy_auto WHERE cat != ''"):
        if pic not in first or order.index(cat) < order.index(first[pic]):
            first[pic] = cat
    sup_only = {r[0]: r[1] for r in con.execute("SELECT picture, super FROM taxonomy_auto WHERE cat = ''")}
    queues = defaultdict(list)
    for pic, sources in con.execute("SELECT DISTINCT r.picture, p.sources FROM reviews r JOIN pictures p ON p.picture = r.picture "
                                    "WHERE r.decision IN ('illusion', 'unsure') ORDER BY r.picture"):
        if pic in first:
            queues[f"{first[pic]} {CATS[first[pic]]}"].append(pic)
        elif sup_only.get(pic) == "out":
            queues["対象外の候補"].append(pic)
        else:
            queues[f"未分類:{json.loads(sources)[0]}"].append(pic)      # by source, so similar pictures come together
    con.execute("DELETE FROM taxonomy_queue")
    con.executemany("INSERT INTO taxonomy_queue VALUES (?, ?, ?)",
                    [(q, p, i) for q, pics in queues.items() for i, p in enumerate(pics)])
    con.commit()
    n_pics = len({a[0] for a in auto})
    log.info("taxonomy: initial labels on %d pictures; %d queued for category review in %d queues",
             n_pics, sum(len(v) for v in queues.values()), len(queues))


# ------------------------------------------------------------------ web pages (mounted in review.serve)

def page_start(app, reviewer: str) -> str:
    con = app.db()
    rows = con.execute("""SELECT q.queue, count(*) n,
                                 sum(EXISTS (SELECT 1 FROM taxonomy_reviews r WHERE r.picture = q.picture)) done
                          FROM taxonomy_queue q GROUP BY q.queue""").fetchall()
    con.close()
    order = {f"{k} {v}": i for i, (k, v) in enumerate(CATS.items())}
    rows = sorted(rows, key=lambda r: order.get(r["queue"], 99))
    t = app.token
    links = "".join(f"<tr><td><a href='/tax/grid?token={t}&reviewer={quote(reviewer)}&queue={quote(r['queue'])}'>"
                    f"{escape(r['queue'])}</a></td><td>{r['done']}</td><td>{r['n']}</td></tr>" for r in rows)
    legend = "".join(f"<li><b>{k}</b> {v}</li>" for k, v in CATS.items())
    return f"""<title>錯視の分類</title><style>{CSS}</style>
<h1>錯視の分類</h1>
<form method=get action=/tax><input type=hidden name=token value={t}>
確認する人の名前: <input name=reviewer value="{escape(reviewer)}" required> <button>決定</button></form>
<p>「錯視」と判断された絵に、分類を付けます。最初に付いている印は、錯視名の辞書と収集元の分類から Claude が付けた初期値です。
印をクリックで付け外しします。複数付けてかまいません。ダブルクリックで原寸を別タブで開きます。</p>
<ul class=legend>{legend}<li><b>対象外</b> 錯視でもだまし絵でもない</li><li><b>?</b> 判断保留</li></ul>
<p>錯視 = 答えが脳か目にあるもの。<b>T だまし絵・トリックアート</b>は答えが明快で絵の作り方の問題(錯視の大分類と両方付けても可)。
並びは初期値の最初の分類ごと。</p>
{"<table><tr><th>並び</th><th>確認済み</th><th>件数</th></tr>" + links + "</table>" if reviewer else "<p>名前を入れてください。</p>"}
<p><a href='/?token={t}&reviewer={quote(reviewer)}'>錯視かどうかの確認へ</a></p>"""


def page_grid(app, queue: str, reviewer: str) -> str:
    import time
    con = app.db()
    now = time.time()
    with app.lock:
        locked = {p for p, (who, until) in app.locks.items() if until > now and who != reviewer}
        rows = con.execute("""SELECT q.picture, p.rep_sha, p.title, p.sources FROM taxonomy_queue q
                              JOIN pictures p ON p.picture = q.picture
                              WHERE q.queue = ? AND NOT EXISTS (SELECT 1 FROM taxonomy_reviews r WHERE r.picture = q.picture)
                              ORDER BY q.ord""", (queue,)).fetchall()
        batch = [r for r in rows if r["picture"] not in locked][:PAGE_SIZE]
        for r in batch:
            app.locks[r["picture"]] = (reviewer, now + 20 * 60)
    initial = defaultdict(set)
    for r in batch:
        for (cat, sup) in con.execute("SELECT cat, super FROM taxonomy_auto WHERE picture = ?", (r["picture"],)):
            initial[r["picture"]].add(cat or (OUT if sup == "out" else ""))
    con.close()
    t = app.token
    if not batch:
        return (f"<style>{CSS}</style><p>この並びで確認できる絵はもうありません。</p>"
                f"<p><a href='/tax?token={t}&reviewer={quote(reviewer)}'>一覧へ戻る</a></p>")
    keys = list(CATS) + [OUT, "unsure"]
    label = {OUT: "対象外", "unsure": "?"}

    def chips(pic):
        on = initial[pic]
        return "".join(f"<span class='k{' on' if k in on else ''}' data-k='{k}' title='{escape(CATS.get(k, label.get(k, k)))}'>"
                       f"{label.get(k, k)}</span>" for k in keys)
    tiles = "".join(
        f"<div class=t data-p='{r['picture']}' title='{escape((r['title'] or '')[:200])} / {escape(', '.join(json.loads(r['sources'])))}'>"
        f"<img loading=lazy src='/thumb/{r['rep_sha']}?token={t}' ondblclick=\"window.open('/full/{r['rep_sha']}?token={t}')\">"
        f"<div class=c>{escape((r['title'] or '')[:60])}</div><div class=ks>{chips(r['picture'])}</div></div>"
        for r in batch)
    legend = " ".join(f"<b>{k}</b>{v}" for k, v in CATS.items())
    return f"""<title>錯視の分類 - {escape(queue)}</title><style>{CSS}</style>
<div class=bar><b>{escape(queue)}</b> 確認者: {escape(reviewer)} / この画面 {len(batch)} 枚
<button onclick=save()>保存して次へ</button> <a href='/tax?token={t}&reviewer={quote(reviewer)}'>一覧へ</a>
<div class=lg>{legend} <b>対象外</b> <b>?</b>保留</div></div>
<div class=grid>{tiles}</div>
<script>
document.querySelectorAll('.k').forEach(e=>e.onclick=()=>e.classList.toggle('on'));
function save(){{
  const d={{}}; document.querySelectorAll('.t').forEach(t=>d[t.dataset.p]=[...t.querySelectorAll('.k.on')].map(e=>e.dataset.k));
  fetch('/tax/submit?token={t}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{reviewer:{json.dumps(reviewer)},queue:{json.dumps(queue)},decisions:d}})}})
  .then(r=>{{if(r.ok) location.reload(); else alert('保存できませんでした');}});}}
</script>"""


def submit(app, body: dict) -> None:
    reviewer = (body.get("reviewer") or "").strip()[:60]
    if not reviewer:
        raise ValueError("no reviewer")
    allowed = set(CATS) | {OUT, "unsure"}
    rows = [(p, reviewer, json.dumps([k for k in ks if k in allowed]), body.get("queue"))
            for p, ks in (body.get("decisions") or {}).items()]
    con = app.db()
    con.executemany("INSERT OR REPLACE INTO taxonomy_reviews (picture, reviewer, cats, queue, at) "
                    "VALUES (?, ?, ?, ?, datetime('now'))", rows)
    con.commit()
    con.close()
    with app.lock:
        for p, *_ in rows:
            app.locks.pop(p, None)


CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.t{border:2px solid #bbb;border-radius:6px;background:#fff;padding:4px}
.t img{width:100%;height:190px;object-fit:contain;display:block;background:#eee;cursor:zoom-in}
.t .c{font-size:11px;color:#555;height:28px;overflow:hidden}
.ks{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px}
.k{border:1px solid #888;border-radius:4px;padding:1px 6px;font-size:13px;cursor:pointer;user-select:none;background:#fff}
.k.on{background:#1565c0;color:#fff;border-color:#1565c0}
.k[data-k=T].on{background:#6a1b9a;border-color:#6a1b9a}.k[data-k=out].on{background:#c62828;border-color:#c62828}
.k[data-k=unsure].on{background:#f9a825;border-color:#f9a825;color:#000}
.bar{position:sticky;top:0;background:#fafafa;padding:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:1}
.lg{font-size:12px;color:#444;width:100%} .lg b{margin-left:8px}
.legend{columns:2;font-size:14px}
button{font-size:16px;padding:6px 16px} table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:3px 8px;text-align:right}
td:first-child,th:first-child{text-align:left}
"""


# --- Claude's blind main categories (2026-10-08) -------------------------------------------------------------
# Kept apart from taxonomy_reviews (humans). One row per picture and model; cats is a JSON list of letters.
MACHINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS taxonomy_machine (
    picture   TEXT NOT NULL,
    model     TEXT NOT NULL,
    cats      TEXT NOT NULL,           -- JSON list: CATS letters, 'T', 'none' or '?'
    main      TEXT,
    name      TEXT,                    -- established illusion name, if recognised
    note      TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, model)
);
"""
VALID = set(CATS) | {"none", "?"}


def store_machine(con, manifest: dict, answers: list[dict], model: str) -> int:
    con.executescript(MACHINE_SCHEMA)
    rows = []
    for a in answers:
        pic = manifest.get(str(a.get("sheet")), {}).get(str(a.get("number")))
        cats = [c for c in (a.get("cats") or []) if c in VALID]
        if pic and cats:
            main = a.get("main") if a.get("main") in cats else cats[0]
            rows.append((pic, model, json.dumps(cats), main, (a.get("name") or "")[:120], (a.get("note") or "")[:300]))
    con.executemany("INSERT OR REPLACE INTO taxonomy_machine (picture, model, cats, main, name, note, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))", rows)
    con.commit()
    return len(rows)
