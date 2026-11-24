"""Human review of "is this an illusion?" — data preparation and a small LAN web app.

Unit of review = a *picture*: all image files that look the same (identical content or near-duplicates in
image_matches) form one picture, so one decision covers thumbnails and reposts.

Queues:
  C:<source>        every picture whose best evidence is a search hit (Openverse) or an unlinked mixed source
  A:<source> / B:<source>  a random sample (SAMPLE_PER_SOURCE) per source of curated (A) or dictionary-linked (B)
                    pictures, to estimate precision; a source that is nearly all illusions is accepted as a whole.
Decisions: illusion | not_illusion | unsure, one row per (picture, reviewer). Pictures are offered to whoever
has seen the fewest reviews; AGREEMENT_SHARE of reviewed pictures are offered once more to a second person.
"""
import hashlib
import json
import logging
import random
import sqlite3
from collections import defaultdict

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pictures (
    picture     TEXT PRIMARY KEY,      -- id: sha256 of the representative (largest) file
    rep_sha     TEXT NOT NULL,
    tier        TEXT NOT NULL,         -- A curated | B linked | C searched/unlinked
    sources     TEXT NOT NULL,         -- JSON list
    n_files     INTEGER NOT NULL,
    title       TEXT
);
CREATE TABLE IF NOT EXISTS picture_files (
    sha256      TEXT PRIMARY KEY,
    picture     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
    picture     TEXT NOT NULL,
    queue       TEXT NOT NULL,         -- e.g. 'C:openverse', 'A:kitaoka'
    ord         INTEGER NOT NULL,
    PRIMARY KEY (queue, picture)
);
CREATE TABLE IF NOT EXISTS reviews (
    picture     TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    decision    TEXT NOT NULL,         -- illusion | not_illusion | unsure
    queue       TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (picture, reviewer)
);
"""

SEARCHED = {"openverse"}
MIXED = {"commons", "kodaka"}
SKIP_FLAGS = {"thumbnail", "likely_navigation", "not_illusion"}
SAMPLE_PER_SOURCE = 200
# larger sample where the share of non-works is unknown (2026-10-06): if it turns out high, review it in full
SAMPLE_SIZES = {"A:kitaoka(年月日なし)": None}   # None = in full (user, 2026-10-07: the 500 sample was 61% illusion)
QUERY_SAMPLE = 30                    # per Openverse query (S:openverse:<query>)
TIER_ORDER = {"A": 0, "B": 1, "C": 2}


def prepare(con: sqlite3.Connection, seed: int = 20261006) -> None:
    """(Re)build pictures and queues. Existing reviews are kept (they refer to picture ids = representative sha)."""
    con.executescript(SCHEMA)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    size = {}
    for sha, w, h in con.execute("SELECT sha256, coalesce(width, 0), coalesce(height, 0) FROM images"):
        parent.setdefault(sha, sha)
        size[sha] = w * h
    for a, b in con.execute("SELECT sha_a, sha_b FROM image_matches"):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    flags = defaultdict(set)
    for item_id, flag in con.execute("SELECT item_id, flag FROM item_flags"):
        flags[item_id].add(flag)
    linked = {r[0] for r in con.execute("SELECT DISTINCT item_id FROM item_illusions")}

    from .config import PROJECT_ROOT
    from .openverse import GENERIC_QUERIES
    full_file = PROJECT_ROOT / "sources" / "review_openverse_full.txt"
    full_queries = {l.strip() for l in full_file.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#")} if full_file.exists() else set()
    groups: dict[str, dict] = {}
    for item_id, source, sha, title, dated, query in con.execute(
            "SELECT id, source, image_sha256, title, date_created IS NOT NULL, "
            "json_extract(raw_json, '$.extra.query') FROM items WHERE status = 'stored'"):
        if flags[item_id] & SKIP_FLAGS:
            continue
        root = find(sha)
        if source in SEARCHED:
            tier = "B" if item_id in linked else "C"
        elif source in MIXED:
            tier = "B" if item_id in linked else "C"
        else:
            tier = "A"
        g = groups.setdefault(root, {"files": set(), "sources": set(), "tier": tier, "titles": [], "by_source": {}})
        g["files"].add(sha)
        g["sources"].add(source)
        g["titles"].append(title)
        if source == "openverse" and query:
            g.setdefault("queries", set()).add(query)
        if source == "kitaoka":
            # Kitaoka: works dated by a Copyright line vs everything else on the site (photos, covers, figures)
            g["kitaoka_dated"] = g.get("kitaoka_dated", False) or bool(dated)
        g["by_source"].setdefault(source, tier)
        if TIER_ORDER[tier] < TIER_ORDER[g["tier"]]:
            g["tier"] = tier

    con.execute("DELETE FROM pictures")
    con.execute("DELETE FROM picture_files")
    con.execute("DELETE FROM review_queue")
    # picture ids must survive rebuilds, because reviews refer to them: keep an id that already has reviews
    reviewed = {r[0] for r in con.execute("SELECT DISTINCT picture FROM reviews")}
    rows, files = [], []
    for root, g in groups.items():
        rep = max(sorted(g["files"]), key=lambda s: size.get(s, 0))       # sorted: deterministic on ties
        pid = min(g["files"] & reviewed, default=rep)
        title = next((t for t in g["titles"] if t), None)
        rows.append((pid, rep, g["tier"], json.dumps(sorted(g["sources"])), len(g["files"]), title))
        files += [(s, pid) for s in g["files"]]
        g["pid"] = pid
    con.executemany("INSERT INTO pictures VALUES (?, ?, ?, ?, ?, ?)", rows)
    # every file of a picture (including skipped thumbnails) points to it
    for sha in parent:
        root = find(sha)
        if root in groups:
            files.append((sha, groups[root]["pid"]))
    con.executemany("INSERT OR IGNORE INTO picture_files VALUES (?, ?)", files)

    reviewed_in = defaultdict(list)                    # queue -> pictures already reviewed there, in review order
    for pic, q in con.execute("SELECT picture, queue FROM reviews ORDER BY at, rowid"):
        if pic not in reviewed_in[q]:
            reviewed_in[q].append(pic)
    queue_rows = []
    by_queue: dict[str, list[str]] = defaultdict(list)
    for g in groups.values():
        if g["tier"] == "C":
            src = sorted(g["sources"])[0]
            if src == "openverse":
                # Openverse is a keyword search: generic illusion words are reviewed in full; results of a
                # concept name ("The dress", "Spinning Dancer") are sampled per query first, because most are
                # ordinary photos carrying that word. Queries listed in review_openverse_full.txt go in full.
                qs = sorted(g.get("queries", []))
                generic = [q for q in qs if q in GENERIC_QUERIES]
                full = [q for q in qs if q in full_queries]
                if generic:
                    by_queue["C:openverse(錯視の総称)"].append(g["pid"])
                elif full:
                    by_queue[f"C:openverse:{full[0]}"].append(g["pid"])
                else:
                    by_queue[f"S:openverse:{qs[0] if qs else '?'}"].append(g["pid"])
                continue
            by_queue[f"C:{src}"].append(g["pid"])
        else:
            for src, tier in g["by_source"].items():
                if tier == g["tier"]:
                    if src == "kitaoka":
                        src = "kitaoka(年月日あり)" if g.get("kitaoka_dated") else "kitaoka(年月日なし)"
                    by_queue[f"{g['tier']}:{src}"].append(g["pid"])
    for q, pids in by_queue.items():
        pids = sorted(set(pids))
        random.Random(f"{seed}:{q}").shuffle(pids)     # one generator per queue: other queues cannot shift a sample
        members = set(pids)
        prior = [p for p in reviewed_in[q] if p in members]
        prior += [p for p in pids if p in reviewed and p not in set(prior)]   # reviewed via another queue it shares
        prior_set = set(prior)
        pids = prior + [p for p in pids if p not in prior_set]   # already-reviewed pictures stay in their sample
        if not q.startswith("C:"):
            pids = pids[:SAMPLE_SIZES.get(q, QUERY_SAMPLE if q.startswith("S:") else SAMPLE_PER_SOURCE)]
        queue_rows += [(p, q, i) for i, p in enumerate(pids)]
    con.executemany("INSERT INTO review_queue VALUES (?, ?, ?)", queue_rows)
    con.commit()
    tiers = defaultdict(int)
    for r in rows:
        tiers[r[2]] += 1
    log.info("review: %d pictures %s, %d queued in %d queues", len(rows), dict(tiers), len(queue_rows), len(by_queue))


def token_for(path) -> str:
    """The review URL token (created once, kept in ~/.config with mode 600)."""
    import os
    from pathlib import Path
    p = Path(os.path.expanduser(path))
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(hashlib.sha256(os.urandom(32)).hexdigest()[:24])
        p.chmod(0o600)
    return p.read_text().strip()


# ---------------------------------------------------------------- web app

import html
import ipaddress
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

PAGE_SIZE = 60
LOCK_SECONDS = 20 * 60
AGREEMENT_EVERY = 10                 # every 10th picture of a queue is also shown to a second reviewer
ALLOWED_NETS = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8")]   # localhost, local network (edit for yours)
DECISIONS = ("illusion", "not_illusion", "unsure")

CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.t{border:5px solid #2e7d32;border-radius:6px;background:#fff;padding:4px;cursor:pointer;user-select:none}
.t img{width:100%;height:150px;object-fit:contain;display:block;background:#eee}
.t .c{font-size:11px;color:#555;height:28px;overflow:hidden}
.t.not_illusion{border-color:#c62828}.t.unsure{border-color:#f9a825}
.bar{position:sticky;top:0;background:#fafafa;padding:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
button{font-size:16px;padding:6px 16px} table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:3px 8px;text-align:right}
td:first-child,th:first-child{text-align:left}
"""


class ReviewApp:
    def __init__(self, s, db_path: Path, token: str):
        self.s, self.db_path, self.token = s, db_path, token
        self.thumb_dir = s.work_dir / "cache" / "thumbs"
        self.locks: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()
        from . import storage
        self.storage = storage

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=60)
        con.row_factory = sqlite3.Row
        return con

    # -------------------------------------------------------- data

    def next_batch(self, con, queue: str, reviewer: str) -> list[sqlite3.Row]:
        now = time.time()
        with self.lock:
            locked = {p for p, (who, until) in self.locks.items() if until > now and who != reviewer}
            # "S:*" = every S queue at once (small leftovers in one screen); pictures stay grouped by their queue
            where, order = ("q.queue LIKE ?", "n, q.queue, q.ord") if queue.endswith(":*") else ("q.queue = ?", "n, q.ord")
            rows = con.execute(
                f"""SELECT q.picture, q.queue, q.ord, p.rep_sha, p.title, p.sources,
                          (SELECT count(*) FROM reviews r WHERE r.picture = q.picture) n
                   FROM review_queue q JOIN pictures p ON p.picture = q.picture
                   WHERE {where} AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.picture = q.picture AND r.reviewer = ?)
                   ORDER BY {order}""", (queue[:-1] + "%" if queue.endswith(":*") else queue, reviewer)).fetchall()
            seen = set()
            rows = [r for r in rows if not (r["picture"] in seen or seen.add(r["picture"]))]
            batch = [r for r in rows if r["picture"] not in locked
                     and (r["n"] == 0 or (r["n"] == 1 and r["ord"] % AGREEMENT_EVERY == 0))][:PAGE_SIZE]
            for r in batch:
                self.locks[r["picture"]] = (reviewer, now + LOCK_SECONDS)
        return batch

    def thumb(self, sha: str) -> tuple[bytes, str]:
        con = self.db()
        row = con.execute("SELECT disk, rel_path, mime, bytes, frames FROM images WHERE sha256 = ?", (sha,)).fetchone()
        con.close()
        if row is None:
            raise FileNotFoundError(sha)
        path = self.storage.disk_root(self.s, row["disk"]) / row["rel_path"]
        if row["mime"] == "image/svg+xml" or ((row["frames"] or 1) > 1 and row["bytes"] < 3_000_000):
            return path.read_bytes(), row["mime"]          # keep SVG and (small) animations as they are
        cached = self.thumb_dir / sha[:2] / f"{sha}.jpg"
        if not cached.exists():
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(path) as im:
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((320, 320))
                cached.parent.mkdir(parents=True, exist_ok=True)
                buf = BytesIO()
                im.save(buf, "JPEG", quality=82)
                cached.write_bytes(buf.getvalue())
        return cached.read_bytes(), "image/jpeg"

    def full(self, sha: str) -> tuple[bytes, str]:
        con = self.db()
        row = con.execute("SELECT disk, rel_path, mime FROM images WHERE sha256 = ?", (sha,)).fetchone()
        con.close()
        return (self.storage.disk_root(self.s, row["disk"]) / row["rel_path"]).read_bytes(), row["mime"]

    # -------------------------------------------------------- pages

    def page_start(self, reviewer: str) -> str:
        con = self.db()
        rows = con.execute(
            """SELECT q.queue, count(*) n,
                      sum(EXISTS (SELECT 1 FROM reviews r WHERE r.picture = q.picture)) done
               FROM review_queue q GROUP BY q.queue
               ORDER BY instr('CSBA', substr(q.queue, 1, 1)), n DESC""").fetchall()
        con.close()
        combined = {}
        for r in rows:
            tier = r["queue"].split(":", 1)[0]
            n, done, k = combined.get(tier, (0, 0, 0))
            combined[tier] = (n + r["n"], done + r["done"], k + 1)
        t = self.token
        links = "".join(
            f"<tr><td><a href='/grid?token={t}&reviewer={quote(reviewer)}&queue={quote(tier + ':*')}'><b>{tier}:* "
            f"({k}の並びをまとめて)</b></a></td><td>{done}</td><td>{n}</td></tr>"
            for tier, (n, done, k) in combined.items() if k > 1 and done < n) + "".join(
            f"<tr><td><a href='/grid?token={t}&reviewer={quote(reviewer)}&queue={quote(r['queue'])}'>{html.escape(r['queue'])}</a></td>"
            f"<td>{r['done']}</td><td>{r['n']}</td></tr>" for r in rows
            if not (lambda n, done, k: k > 1 and done < n)(*combined[r["queue"].split(":", 1)[0]]))   # shown as "S:*" only
        name = html.escape(reviewer)
        return f"""<title>錯視の確認</title><style>{CSS}</style>
<h1>錯視かどうかの確認</h1>
<form method=get action=/><input type=hidden name=token value={t}>
確認する人の名前: <input name=reviewer value="{name}" required> <button>決定</button></form>
<p>画像はすべて最初「錯視」(緑)になっています。錯視でないものをクリックすると<b style=color:#c62828>赤(錯視でない)</b>、
もう一度クリックで<b style=color:#f9a825>黄(判断保留)</b>、もう一度で緑に戻ります。ダブルクリックで原寸を別タブで開きます。
Enter キーまたは「保存して次へ」で次の画面へ進みます。「全部 錯視でない」(N キー)で全部を赤に、「全部 錯視」(Y キー)で全部を緑にできます(赤にしてから錯視だけクリックで戻す使い方が速い並びもあります)。</p>
<p>C: 全数確認 / S: Openverse の検索語ごとの抜き取り(30枚)/ A・B: 専門サイトと辞書に結び付いた画像の抜き取り。
「S:*」などの太字の行は、同じ種類の並びの残りをまとめて確認します(判断は元の並びごとに記録されます)。</p>
{"<table><tr><th>確認の並び</th><th>確認済み</th><th>件数</th></tr>" + links + "</table>" if reviewer else "<p>名前を入れてください。</p>"}
<p><a href='/tax2?token={t}&reviewer={quote(reviewer)}'><b>錯視の大分類(伏せた確認)</b></a> /
<a href='/tax?token={t}&reviewer={quote(reviewer)}'>収集元の区分から作った分類の並び(旧)</a></p>
<p><a href='/stats?token={t}'>進み具合と一致度</a> / <a href='/compare?token={t}&reviewer={quote(reviewer)}'>人と Claude の比較</a></p>"""

    def page_grid(self, queue: str, reviewer: str) -> str:
        con = self.db()
        batch = self.next_batch(con, queue, reviewer)
        con.close()
        t = self.token
        if not batch:
            return (f"<style>{CSS}</style><p>この並びで確認できる画像はもうありません。</p>"
                    f"<p><a href='/?token={t}&reviewer={quote(reviewer)}'>一覧へ戻る</a></p>")
        tiles = "".join(
            f"<div class=t data-p='{r['picture']}' data-q='{html.escape(r['queue'])}' title='{html.escape((r['title'] or '')[:200])} / "
            f"{html.escape(', '.join(json.loads(r['sources'])))}' ondblclick=\"window.open('/full/{r['rep_sha']}?token={t}')\">"
            f"<img loading=lazy src='/thumb/{r['rep_sha']}?token={t}'><div class=c>{html.escape((r['title'] or '')[:60])}</div></div>"
            for r in batch)
        return f"""<title>錯視の確認 - {html.escape(queue)}</title><style>{CSS}</style>
<div class=bar><b>{html.escape(queue)}</b> 確認者: {html.escape(reviewer)} / この画面 {len(batch)} 枚
<button onclick=save()>保存して次へ (Enter)</button>
<button onclick="setAll('not_illusion')" style="border:2px solid #c62828">全部 錯視でない・赤 (N)</button>
<button onclick="setAll('illusion')" style="border:2px solid #2e7d32">全部 錯視・緑 (Y)</button>
<a href='/?token={t}&reviewer={quote(reviewer)}'>一覧へ</a></div>
<div class=grid>{tiles}</div>
<script>
const states=['illusion','not_illusion','unsure'];
document.querySelectorAll('.t').forEach(e=>{{e.dataset.s='illusion';e.onclick=()=>{{
  const i=(states.indexOf(e.dataset.s)+1)%3; e.classList.remove(e.dataset.s); e.dataset.s=states[i];
  if(i) e.classList.add(states[i]);}};}});
function setAll(s){{document.querySelectorAll('.t').forEach(e=>{{e.classList.remove(e.dataset.s);e.dataset.s=s;
  if(s!=='illusion') e.classList.add(s);}});}}
function save(){{
  const d={{}},qs={{}}; document.querySelectorAll('.t').forEach(e=>{{d[e.dataset.p]=e.dataset.s;qs[e.dataset.p]=e.dataset.q;}});
  fetch('/submit?token={t}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{reviewer:{json.dumps(reviewer)},queue:{json.dumps(queue)},queues:qs,decisions:d}})}})
  .then(r=>{{if(r.ok) location.reload(); else alert('保存できませんでした');}});}}
document.addEventListener('keydown',e=>{{if(e.key==='Enter') save();
  else if(e.key==='n'||e.key==='N') setAll('not_illusion'); else if(e.key==='y'||e.key==='Y') setAll('illusion');}});
</script>"""

    def page_stats(self) -> str:
        con = self.db()
        q = con.execute(
            """SELECT q.queue, count(DISTINCT q.picture) n, count(DISTINCT r.picture) reviewed,
                      sum(r.decision = 'not_illusion') no, sum(r.decision = 'unsure') unsure, count(r.picture) votes
               FROM review_queue q LEFT JOIN reviews r ON r.picture = q.picture
               GROUP BY q.queue ORDER BY substr(q.queue, 1, 1) DESC, n DESC""").fetchall()
        who = con.execute("SELECT reviewer, count(*) n, max(at) last FROM reviews GROUP BY reviewer ORDER BY n DESC").fetchall()
        pairs = con.execute(
            """SELECT a.decision = b.decision same, count(*) n FROM reviews a JOIN reviews b
               ON a.picture = b.picture AND a.reviewer < b.reviewer GROUP BY 1""").fetchall()
        con.close()
        rows = "".join(
            f"<tr><td>{html.escape(r['queue'])}</td><td>{r['n']}</td><td>{r['reviewed']}</td>"
            f"<td>{(100 * (r['no'] or 0) / r['votes']):.0f}%</td><td>{(100 * (r['unsure'] or 0) / r['votes']):.0f}%</td></tr>"
            if r["votes"] else f"<tr><td>{html.escape(r['queue'])}</td><td>{r['n']}</td><td>0</td><td>-</td><td>-</td></tr>"
            for r in q)
        people = "".join(f"<tr><td>{html.escape(r['reviewer'])}</td><td>{r['n']}</td><td>{r['last']}</td></tr>" for r in who)
        agree = {r["same"]: r["n"] for r in pairs}
        total = sum(agree.values())
        agreement = f"{100 * agree.get(1, 0) / total:.0f}% ({total} 組)" if total else "まだ2人分の判断がありません"
        return f"""<title>進み具合</title><style>{CSS}</style><h1>進み具合</h1>
<table><tr><th>確認の並び</th><th>件数</th><th>確認済み</th><th>錯視でない</th><th>保留</th></tr>{rows}</table>
<h2>確認者</h2><table><tr><th>名前</th><th>判断数</th><th>最後</th></tr>{people}</table>
<h2>2人の判断の一致度</h2><p>{agreement}</p><p><a href='/?token={self.token}'>戻る</a></p>"""

    def page_compare(self, reviewer: str, source: str = "") -> str:
        """Human vs blind model judgements on eval_set (opened to the user after their review, 2026-10-07)."""
        con = self.db()
        model = con.execute("SELECT model FROM machine_reviews GROUP BY model ORDER BY count(*) DESC").fetchone()
        reviewer = reviewer or (con.execute("SELECT reviewer FROM reviews GROUP BY reviewer ORDER BY count(*) DESC")
                                .fetchone() or [""])[0]
        rows = con.execute(
            """SELECT e.picture, e.queue, r.decision h, m.decision m, m.note, p.rep_sha, p.title, p.sources
               FROM eval_set e JOIN reviews r ON r.picture = e.picture AND r.reviewer = ?
               JOIN machine_reviews m ON m.picture = e.picture AND m.model = ?
               JOIN pictures p ON p.picture = e.picture ORDER BY e.queue, e.picture""",
            (reviewer, model[0] if model else "")).fetchall()
        con.close()
        t, J = self.token, {"illusion": "錯視", "not_illusion": "錯視でない", "unsure": "保留"}
        group = lambda q: q if q.startswith(("A:", "B:")) else q.split(":")[0] + (":openverse" if "openverse" in q else
                                                                                  ":" + q.split(":")[1] if q.startswith("C:") else "")

        def summary(rs):
            b = [(r["h"], r["m"]) for r in rs if "unsure" not in (r["h"], r["m"])]
            if not b:
                return len(rs), len(b), None, None
            n = len(b); po = sum(h == m for h, m in b) / n
            ph = sum(h == "illusion" for h, _ in b) / n; pm = sum(m == "illusion" for _, m in b) / n
            pe = ph * pm + (1 - ph) * (1 - pm)
            return len(rs), n, po, (po - pe) / (1 - pe) if pe < 1 else None

        n, nb, po, k = summary(rows)
        cm = "".join(f"<tr><th>人: {J[h]}</th>" + "".join(
            f"<td>{sum(1 for r in rows if r['h'] == h and r['m'] == m)}</td>" for m in J) + "</tr>" for h in J)
        by = defaultdict(list)
        for r in rows:
            by[group(r["queue"])].append(r)
        per = ""
        for g, rs in sorted(by.items(), key=lambda kv: (summary(kv[1])[2] if summary(kv[1])[2] is not None else 9)):
            a, b, pa, kk = summary(rs)
            hi = sum(r["h"] == "illusion" for r in rs) / a; mi = sum(r["m"] == "illusion" for r in rs) / a
            per += (f"<tr><td><a href='/compare?token={t}&reviewer={quote(reviewer)}&source={quote(g)}#diff'>{html.escape(g)}</a></td>"
                    f"<td>{a}</td><td>{'' if pa is None else f'{pa:.0%}'}</td><td>{'' if kk is None else f'{kk:.2f}'}</td>"
                    f"<td>{hi:.0%}</td><td>{mi:.0%}</td></tr>")

        sel = [r for r in rows if not source or group(r["queue"]) == source]
        def tiles(cond, label):
            rs = [r for r in sel if cond(r)]
            if not rs:
                return ""
            return (f"<h3>{label}({len(rs)})</h3><div class=grid>" + "".join(
                f"<div class=t style='border-color:#999' ondblclick=\"window.open('/full/{r['rep_sha']}?token={t}')\" "
                f"title='{html.escape((r['title'] or '')[:200])}'><img loading=lazy src='/thumb/{r['rep_sha']}?token={t}'>"
                f"<div class=c>人: <b>{J[r['h']]}</b> / Claude: <b>{J[r['m']]}</b><br>Claude のメモ: {html.escape(r['note'] or '-')}"
                f"<br>{html.escape(', '.join(json.loads(r['sources'])))}</div></div>" for r in rs) + "</div>")
        diff = (tiles(lambda r: r["h"] == "illusion" and r["m"] == "not_illusion", "人: 錯視 / Claude: 錯視でない")
                + tiles(lambda r: r["h"] == "not_illusion" and r["m"] == "illusion", "人: 錯視でない / Claude: 錯視")
                + tiles(lambda r: r["m"] == "unsure" or r["h"] == "unsure", "どちらかが保留")
                + (tiles(lambda r: r["h"] == r["m"] != "unsure", "一致") if source else ""))
        return f"""<title>人と Claude の比較</title><style>{CSS}.t .c{{height:auto;max-height:70px}}</style>
<h1>人と Claude の判断の比較</h1>
<p>比較用の {n} 枚(<code>eval_set</code>、126の並びから層別に抜き取り、人の判断が0件の時点で固定)。
人: {html.escape(reviewer)}。Claude: {html.escape(model[0] if model else '-')}(会話の文脈を持たないサブエージェントに、
番号付きの一覧画像だけを見せて判定。題名・収集元・人の判断は渡していない)。</p>
<h2>全体</h2><table><tr><th></th>{"".join(f"<th>Claude: {v}</th>" for v in J.values())}</tr>{cm}</table>
<p>両方が錯視/錯視でないと答えた {nb} 枚で一致 {po:.1%}、カッパ {k:.2f}</p>
<h2>収集元ごと(一致の低い順)</h2><p>名前をクリックすると、その収集元の絵を並べます。</p>
<table><tr><th>並び</th><th>枚数</th><th>一致</th><th>カッパ</th><th>人: 錯視の割合</th><th>Claude: 錯視の割合</th></tr>{per}</table>
<h2 id=diff>判断が分かれた絵{f'({html.escape(source)}、一致した絵も表示)' if source else '(全体)'}</h2>
<p>{f"<a href='/compare?token={t}&reviewer={quote(reviewer)}#diff'>全体に戻る</a>" if source else ""} ダブルクリックで原寸。</p>
{diff}<p><a href='/?token={t}&reviewer={quote(reviewer)}'>一覧へ</a></p>"""

    def submit(self, body: dict) -> None:
        reviewer = (body.get("reviewer") or "").strip()[:60]
        if not reviewer:
            raise ValueError("no reviewer")
        queues = body.get("queues") or {}                 # per-picture real queue (combined "S:*" screens)
        rows = [(p, reviewer, d, queues.get(p) or body.get("queue")) for p, d in (body.get("decisions") or {}).items() if d in DECISIONS]
        con = self.db()
        con.executemany("INSERT OR REPLACE INTO reviews (picture, reviewer, decision, queue, at) "
                        "VALUES (?, ?, ?, ?, datetime('now'))", rows)
        con.commit()
        con.close()
        with self.lock:
            for p, *_ in rows:
                self.locks.pop(p, None)


def serve(s, db_path: Path, host: str = "0.0.0.0", port: int = 8765) -> None:
    from . import review_tax2, review_v2, taxonomy
    token = token_for("~/.config/claude-illusions/review_token")
    app = ReviewApp(s, db_path, token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("review %s - %s", self.client_address[0], fmt % args)

        def _allowed(self, qs) -> bool:
            ip = ipaddress.ip_address(self.client_address[0])
            return any(ip in n for n in ALLOWED_NETS) and qs.get("token", [""])[0] == token

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600" if ctype.startswith("image") else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._allowed(qs):
                return self._send(403, b"forbidden")
            arg = lambda k: qs.get(k, [""])[0]
            try:
                if u.path == "/":
                    return self._send(200, app.page_start(arg("reviewer")).encode())
                if u.path == "/grid":
                    return self._send(200, app.page_grid(arg("queue"), arg("reviewer")).encode())
                if u.path == "/v2":
                    return self._send(200, review_v2.page_grid(app, arg("reviewer")).encode())
                if u.path == "/tax2":
                    return self._send(200, review_tax2.page_grid(app, arg("reviewer")).encode())
                if u.path == "/tax":
                    return self._send(200, taxonomy.page_start(app, arg("reviewer")).encode())
                if u.path == "/tax/grid":
                    return self._send(200, taxonomy.page_grid(app, arg("queue"), arg("reviewer")).encode())
                if u.path == "/compare":
                    return self._send(200, app.page_compare(arg("reviewer"), arg("source")).encode())
                if u.path == "/stats":
                    return self._send(200, app.page_stats().encode())
                if u.path.startswith("/thumb/"):
                    data, ctype = app.thumb(u.path.split("/")[-1])
                    return self._send(200, data, ctype)
                if u.path.startswith("/full/"):
                    data, ctype = app.full(u.path.split("/")[-1])
                    return self._send(200, data, ctype)
                return self._send(404, b"not found")
            except Exception as e:
                log.warning("review GET %s failed: %s", u.path, e)
                return self._send(500, str(e).encode())

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._allowed(qs) or u.path not in ("/submit", "/tax/submit", "/v2/submit", "/tax2/submit"):
                return self._send(403, b"forbidden")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if u.path == "/v2/submit":
                    review_v2.submit(app, body)
                elif u.path == "/tax2/submit":
                    review_tax2.submit(app, body)
                else:
                    (taxonomy.submit(app, body) if u.path == "/tax/submit" else app.submit(body))
                return self._send(200, b"ok", "text/plain")
            except Exception as e:
                return self._send(400, str(e).encode(), "text/plain")

    log.info("review server on http://%s:%d/?token=%s", host, port, token)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
