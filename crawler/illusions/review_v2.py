"""Human check of Claude's v2 judgements (2026-10-08, the user's reset).

"Illusion" now means a pure illusion: trick art / trompe-l'oeil is not an illusion (kept as its own answer).
prepare() draws 100 pictures from each v2 decision (illusion / not_illusion / unsure), mixes them in one
random order and stores them in v2_check_queue. The page never shows which group a picture came from, and
nothing is preselected: every picture must be answered before the screen can be saved.
Answers go to v2_human (separate from the earlier `reviews`, which used the broader definition).
"""
import json
import random
import sqlite3
import time
from html import escape
from urllib.parse import quote

MODELS = ("claude-opus-5-5/blind-v2", "claude-opus-5-5/blind-v2-pilot")
PER_GROUP = 100
PAGE_SIZE = 30
ANSWERS = {"illusion": "錯視", "not_illusion": "錯視じゃない", "trick": "だまし絵", "unsure": "分からない"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_check_queue (
    picture   TEXT PRIMARY KEY,
    ord       INTEGER NOT NULL,
    machine   TEXT NOT NULL            -- Claude's v2 decision; never shown on the page
);
CREATE TABLE IF NOT EXISTS v2_human (
    picture   TEXT NOT NULL,
    reviewer  TEXT NOT NULL,
    decision  TEXT NOT NULL,           -- illusion | not_illusion | trick | unsure
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, reviewer)
);
"""


def prepare(con: sqlite3.Connection, seed: int = 20261008) -> None:
    con.executescript(SCHEMA)
    if con.execute("SELECT count(*) FROM v2_check_queue").fetchone()[0]:
        print("v2_check_queue already exists; not redrawn (answers refer to it)")
        return
    rng = random.Random(seed)
    picks = []
    for decision in ("illusion", "not_illusion", "unsure"):
        pics = sorted(r[0] for r in con.execute(
            f"SELECT DISTINCT picture FROM machine_reviews WHERE decision = ? AND model IN ({','.join('?' * len(MODELS))})",
            (decision, *MODELS)))
        picks += [(p, decision) for p in rng.sample(pics, PER_GROUP)]
    rng.shuffle(picks)
    con.executemany("INSERT INTO v2_check_queue VALUES (?, ?, ?)", [(p, i, d) for i, (p, d) in enumerate(picks)])
    con.commit()
    print(f"v2 check: {len(picks)} pictures queued")


def page_grid(app, reviewer: str) -> str:
    t = app.token
    if not reviewer:
        return f"""<title>錯視の判定(新しい定義)</title><style>{CSS}</style><h1>錯視の判定(新しい定義)</h1>
<form method=get action=/v2><input type=hidden name=token value={t}>確認する人の名前: <input name=reviewer required> <button>決定</button></form>"""
    con = app.db()
    now = time.time()
    with app.lock:
        locked = {p for p, (who, until) in app.locks.items() if until > now and who != reviewer}
        rows = con.execute("""SELECT q.picture, p.rep_sha FROM v2_check_queue q JOIN pictures p ON p.picture = q.picture
                              WHERE NOT EXISTS (SELECT 1 FROM v2_human h WHERE h.picture = q.picture AND h.reviewer = ?)
                              ORDER BY q.ord""", (reviewer,)).fetchall()
        batch = [r for r in rows if r["picture"] not in locked][:PAGE_SIZE]
        for r in batch:
            app.locks[r["picture"]] = (reviewer, now + 20 * 60)
    done = con.execute("SELECT count(*) FROM v2_human WHERE reviewer = ?", (reviewer,)).fetchone()[0]
    total = con.execute("SELECT count(*) FROM v2_check_queue").fetchone()[0]
    con.close()
    if not batch:
        return f"<style>{CSS}</style><p>{escape(reviewer)} さんの分はすべて終わりました({done} 枚)。ありがとうございました。</p>"
    buttons = "".join(f"<span class=b data-a='{a}'>{label}</span>" for a, label in ANSWERS.items())
    tiles = "".join(
        f"<div class=t data-p='{r['picture']}'><img loading=lazy src='/thumb/{r['rep_sha']}?token={t}' "
        f"ondblclick=\"window.open('/full/{r['rep_sha']}?token={t}')\"><div class=bs>{buttons}</div></div>" for r in batch)
    return f"""<title>錯視の判定(新しい定義)</title><style>{CSS}</style>
<div class=bar><b>錯視の判定(新しい定義)</b> 確認者: {escape(reviewer)} / 済み {done} / {total}
<button id=save onclick=save() disabled>保存して次へ</button> <span id=left></span>
<div class=def><b>錯視</b> = 答えが脳か目にあるもの(実際と違って見える原因が見る側にある)。
<b>だまし絵</b> = トリックアート・トロンプルイユ・3Dアート・遠近法の写真など、答えが明快で絵の作り方の問題(錯視には含めない)。
<b>錯視じゃない</b> = それ以外(普通の写真・絵・図・文字など)。初期値はありません。すべての絵に答えると保存できます。
画像はダブルクリックで原寸。題名や出典はわざと表示していません。</div></div>
<div class=grid>{tiles}</div>
<script>
function upd(){{const n=[...document.querySelectorAll('.t')].filter(t=>!t.dataset.a).length;
  document.getElementById('save').disabled=n>0; document.getElementById('left').textContent=n?('あと '+n+' 枚'):'';}}
document.querySelectorAll('.t').forEach(t=>t.querySelectorAll('.b').forEach(b=>b.onclick=()=>{{
  t.querySelectorAll('.b').forEach(x=>x.classList.remove('on')); b.classList.add('on'); t.dataset.a=b.dataset.a;
  t.className='t '+b.dataset.a; upd();}}));
upd();
function save(){{
  const d={{}}; document.querySelectorAll('.t').forEach(t=>d[t.dataset.p]=t.dataset.a);
  fetch('/v2/submit?token={t}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{reviewer:{json.dumps(reviewer)},decisions:d}})}})
  .then(r=>{{if(r.ok) location.reload(); else alert('保存できませんでした');}});}}
</script>"""


def submit(app, body: dict) -> None:
    reviewer = (body.get("reviewer") or "").strip()[:60]
    if not reviewer:
        raise ValueError("no reviewer")
    rows = [(p, reviewer, a) for p, a in (body.get("decisions") or {}).items() if a in ANSWERS]
    con = app.db()
    con.executemany("INSERT OR REPLACE INTO v2_human (picture, reviewer, decision, at) VALUES (?, ?, ?, datetime('now'))", rows)
    con.commit()
    con.close()
    with app.lock:
        for p, *_ in rows:
            app.locks.pop(p, None)


CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.t{border:5px solid #ddd;border-radius:6px;background:#fff;padding:4px}
.t img{width:100%;height:210px;object-fit:contain;display:block;background:#eee;cursor:zoom-in}
.t.illusion{border-color:#2e7d32}.t.not_illusion{border-color:#c62828}.t.trick{border-color:#6a1b9a}.t.unsure{border-color:#f9a825}
.bs{display:flex;gap:4px;margin-top:4px;flex-wrap:wrap}
.b{flex:1;text-align:center;border:1px solid #888;border-radius:4px;padding:3px 2px;font-size:13px;cursor:pointer;user-select:none}
.b.on[data-a=illusion]{background:#2e7d32;color:#fff}.b.on[data-a=not_illusion]{background:#c62828;color:#fff}
.b.on[data-a=trick]{background:#6a1b9a;color:#fff}.b.on[data-a=unsure]{background:#f9a825}
.bar{position:sticky;top:0;background:#fafafa;padding:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:1}
.def{font-size:13px;color:#333;width:100%}
button{font-size:16px;padding:6px 16px}
"""
