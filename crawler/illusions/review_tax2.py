"""Blind human check of Claude's main categories (2026-10-08, agreed with the user).

prepare() draws up to 30 pictures per Claude main category (all of X, which is small, and 30 of '?') from
taxonomy_machine, mixes them in one random order and stores them in tax2_check_queue. The page shows only the image:
no Claude category, title or source, and nothing preselected (a default would pull the answer, as the first
illusion review's default "illusion" may have). Several categories per picture are allowed; every picture needs at
least one answer before the screen can be saved. Answers go to tax2_human, apart from Claude's taxonomy_machine.
"""
import json
import random
import sqlite3
import time
from html import escape

from .taxonomy import CATS

MODEL = "claude-opus-5-5/tax-blind-1"
PER_GROUP = 30
PAGE_SIZE = 24
SHORT = {"G": "幾何", "L": "明るさ", "C": "色", "M": "動き", "D": "奥行き", "K": "補完", "A": "多義",
         "I": "不可能", "F": "顔", "R": "残像・消失", "X": "多感覚", "T": "だまし絵", "not": "錯視じゃない", "?": "分からない"}
KEYS = [k for k in CATS if k != "T"] + ["T", "not", "?"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS tax2_check_queue (
    picture   TEXT PRIMARY KEY,
    ord       INTEGER NOT NULL,
    machine   TEXT NOT NULL            -- Claude's main category; never shown on the page
);
CREATE TABLE IF NOT EXISTS tax2_human (
    picture   TEXT NOT NULL,
    reviewer  TEXT NOT NULL,
    cats      TEXT NOT NULL,           -- JSON list of KEYS
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, reviewer)
);
"""


def prepare(con: sqlite3.Connection, seed: int = 20261008) -> None:
    con.executescript(SCHEMA)
    if con.execute("SELECT count(*) FROM tax2_check_queue").fetchone()[0]:
        print("tax2_check_queue already exists; not redrawn (answers refer to it)")
        return
    rng = random.Random(seed)
    by_main = {}
    for p, main in con.execute("SELECT picture, main FROM taxonomy_machine WHERE model = ? ORDER BY picture", (MODEL,)):
        by_main.setdefault(main, []).append(p)
    picks = []
    for main, pics in sorted(by_main.items()):
        if main == "none":
            continue
        picks += [(p, main) for p in rng.sample(pics, min(PER_GROUP, len(pics)))]
    rng.shuffle(picks)
    con.executemany("INSERT INTO tax2_check_queue VALUES (?, ?, ?)", [(p, i, m) for i, (p, m) in enumerate(picks)])
    con.commit()
    print(f"tax2 check: {len(picks)} pictures queued")


def page_grid(app, reviewer: str) -> str:
    t = app.token
    if not reviewer:
        return f"""<title>錯視の大分類</title><style>{CSS}</style><h1>錯視の大分類(伏せた確認)</h1>
<form method=get action=/tax2><input type=hidden name=token value={t}>確認する人の名前: <input name=reviewer required> <button>決定</button></form>"""
    con = app.db()
    now = time.time()
    with app.lock:
        locked = {p for p, (who, until) in app.locks.items() if until > now and who != reviewer}
        rows = con.execute("""SELECT q.picture, p.rep_sha FROM tax2_check_queue q JOIN pictures p ON p.picture = q.picture
                              WHERE NOT EXISTS (SELECT 1 FROM tax2_human h WHERE h.picture = q.picture AND h.reviewer = ?)
                              ORDER BY q.ord""", (reviewer,)).fetchall()
        batch = [r for r in rows if r["picture"] not in locked][:PAGE_SIZE]
        for r in batch:
            app.locks[r["picture"]] = (reviewer, now + 30 * 60)
    done = con.execute("SELECT count(*) FROM tax2_human WHERE reviewer = ?", (reviewer,)).fetchone()[0]
    total = con.execute("SELECT count(*) FROM tax2_check_queue").fetchone()[0]
    con.close()
    if not batch:
        return f"<style>{CSS}</style><p>{escape(reviewer)} さんの分はすべて終わりました({done} 枚)。ありがとうございました。</p>"
    chips = "".join(f"<span class='k k{'q' if k == '?' else k}' data-k='{k}'>"
                    f"{k + ' ' if k in CATS and k != 'T' else ''}{SHORT[k]}</span>" for k in KEYS)
    tiles = "".join(
        f"<div class=t data-p='{r['picture']}'><img loading=lazy src='/thumb/{r['rep_sha']}?token={t}' "
        f"ondblclick=\"window.open('/full/{r['rep_sha']}?token={t}')\"><div class=ks>{chips}</div></div>" for r in batch)
    return f"""<title>錯視の大分類</title><style>{CSS}</style>
<div class=bar><b>錯視の大分類(伏せた確認)</b> 確認者: {escape(reviewer)} / 済み {done} / {total}
<button id=save onclick=save() disabled>保存して次へ</button> <span id=left></span>
<div class=def>何が変わって見えるかで選びます。<b>複数選んでよい</b>。初期値はありません。すべての絵に1つ以上選ぶと保存できます。
画像はダブルクリックで原寸。題名・出典・Claude の分類はわざと表示していません。<br>
<b>G</b> 大きさ・長さ・傾き・形 <b>L</b> 明るさ・コントラスト <b>C</b> 色 <b>M</b> 動き(静止画が動く、動きの見え方)
<b>D</b> 奥行き・立体・空間 <b>K</b> 補完・主観的輪郭 <b>A</b> 多義図形(見え方が切り替わる) <b>I</b> 不可能図形
<b>F</b> 顔・物体の認識 <b>R</b> 残像・順応・消失 <b>X</b> マルチモーダル /
<b>だまし絵</b> トリックアート・見立て・二重像・隠し絵 / <b>錯視じゃない</b> / <b>分からない</b></div></div>
<div class=grid>{tiles}</div>
<script>
function upd(){{const n=[...document.querySelectorAll('.t')].filter(t=>!t.querySelector('.k.on')).length;
  document.getElementById('save').disabled=n>0; document.getElementById('left').textContent=n?('あと '+n+' 枚'):'';}}
document.querySelectorAll('.t').forEach(t=>t.querySelectorAll('.k').forEach(k=>k.onclick=()=>{{
  const ex=['not','?'];
  if(ex.includes(k.dataset.k)) t.querySelectorAll('.k').forEach(x=>{{if(x!==k) x.classList.remove('on');}});
  else t.querySelectorAll('.k').forEach(x=>{{if(ex.includes(x.dataset.k)) x.classList.remove('on');}});
  k.classList.toggle('on'); t.classList.toggle('ans', !!t.querySelector('.k.on')); upd();}}));
upd();
function save(){{
  const d={{}}; document.querySelectorAll('.t').forEach(t=>d[t.dataset.p]=[...t.querySelectorAll('.k.on')].map(e=>e.dataset.k));
  fetch('/tax2/submit?token={t}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{reviewer:{json.dumps(reviewer)},decisions:d}})}})
  .then(r=>{{if(r.ok) location.reload(); else alert('保存できませんでした');}});}}
</script>"""


def submit(app, body: dict) -> None:
    reviewer = (body.get("reviewer") or "").strip()[:60]
    if not reviewer:
        raise ValueError("no reviewer")
    rows = []
    for p, ks in (body.get("decisions") or {}).items():
        ks = [k for k in ks if k in KEYS]
        if ks:
            rows.append((p, reviewer, json.dumps(ks)))
    con = app.db()
    con.executemany("INSERT OR REPLACE INTO tax2_human (picture, reviewer, cats, at) VALUES (?, ?, ?, datetime('now'))", rows)
    con.commit()
    con.close()
    with app.lock:
        for p, *_ in rows:
            app.locks.pop(p, None)


CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.t{border:5px solid #ddd;border-radius:6px;background:#fff;padding:4px}
.t.ans{border-color:#2e7d32}
.t img{width:100%;height:220px;object-fit:contain;display:block;background:#eee;cursor:zoom-in}
.ks{display:flex;gap:3px;margin-top:4px;flex-wrap:wrap}
.k{border:1px solid #888;border-radius:4px;padding:2px 4px;font-size:12px;cursor:pointer;user-select:none}
.k.on{background:#1565c0;color:#fff;border-color:#1565c0}
.k.kT.on{background:#6a1b9a;border-color:#6a1b9a}.k.knot.on{background:#c62828;border-color:#c62828}
.k.kq.on{background:#f9a825;color:#222;border-color:#f9a825}
.bar{position:sticky;top:0;background:#fafafa;padding:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:1}
.def{font-size:13px;color:#333;width:100%;line-height:1.6}
button{font-size:16px;padding:6px 16px}
"""
