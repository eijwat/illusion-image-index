"""Model judgements on the fixed evaluation set, blind to human reviews (see CLAUDE.md).

sheets()  renders eval_set pictures, in a seeded random order, onto numbered contact sheets
          (20 per sheet, image only: no title, source or queue). The number -> picture map is written
          next to the sheets in manifest.json, which is NOT given to the judging agents.
store()   reads the agents' answers (sheet, number, decision) and writes machine_reviews.

v2 (2026-10-08, the user's reset): "illusion" means a pure illusion (trick art is not_illusion, tagged 'trick'),
larger tiles (12 per sheet) and several frames of an animation, for any list of pictures.
"""
import io
import json
import logging
import random
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import storage
from .client import PoliteClient
from .config import Settings

log = logging.getLogger(__name__)

COLS, ROWS, TILE = 4, 5, 300
PER_SHEET = COLS * ROWS


def _svg_png(client: PoliteClient, file_url: str) -> bytes:
    """Commons renders SVGs to PNG thumbnails; we have no SVG rasterizer locally."""
    path = file_url.split("?")[0].split("/wikipedia/commons/")[1]          # d/de/Name.svg
    name = path.rsplit("/", 1)[1]
    url = f"https://upload.wikimedia.org/wikipedia/commons/thumb/{path}/500px-{name}.png"   # Commons only serves standard thumbnail sizes (https://w.wiki/GHai)
    r = client.get(url)
    r.raise_for_status()
    return r.content


def sheets(s: Settings, con: sqlite3.Connection, out_dir: Path, seed: int = 7) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute("""SELECT e.picture, i.disk, i.rel_path, i.mime,
                                 (SELECT it.file_url FROM items it WHERE it.image_sha256 = p.rep_sha LIMIT 1) url
                          FROM eval_set e JOIN pictures p ON p.picture = e.picture JOIN images i ON i.sha256 = p.rep_sha
                          ORDER BY e.picture""").fetchall()
    rows = list(rows)
    random.Random(seed).shuffle(rows)                # no grouping by queue or source
    client = PoliteClient(s.user_agent, {"upload.wikimedia.org": 1.0})
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    manifest = {}
    for k in range(0, len(rows), PER_SHEET):
        sheet_no = k // PER_SHEET + 1
        sheet = Image.new("RGB", (COLS * TILE, ROWS * TILE), "white")
        draw = ImageDraw.Draw(sheet)
        manifest[sheet_no] = {}
        for j, r in enumerate(rows[k:k + PER_SHEET]):
            number = j + 1
            x, y = (j % COLS) * TILE, (j // COLS) * TILE
            try:
                if r["mime"] == "image/svg+xml":
                    im = Image.open(io.BytesIO(_svg_png(client, r["url"])))
                else:
                    Image.MAX_IMAGE_PIXELS = None
                    im = Image.open(storage.disk_root(s, r["disk"]) / r["rel_path"])
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((TILE - 12, TILE - 12))
                sheet.paste(im, (x + (TILE - im.width) // 2, y + (TILE - im.height) // 2))
            except Exception as e:
                log.warning("sheet %d #%d: %s", sheet_no, number, e)
                draw.text((x + 60, y + 130), "(cannot render)", fill="red", font=font)
            draw.rectangle([x, y, x + TILE - 1, y + TILE - 1], outline="#888", width=2)
            draw.rectangle([x + 2, y + 2, x + 62, y + 44], fill="black")
            draw.text((x + 8, y + 4), str(number), fill="yellow", font=font)
            manifest[sheet_no][number] = r["picture"]
        sheet.save(out_dir / f"sheet_{sheet_no:03d}.png")
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    log.info("eval sheets: %d sheets, %d pictures -> %s", len(manifest), len(rows), out_dir)


def store(con: sqlite3.Connection, out_dir: Path, answers: list[dict], model: str) -> int:
    """answers: [{"sheet": 1, "number": 3, "decision": "not_illusion", "note": "..."}]"""
    manifest = json.loads((out_dir / "manifest.json").read_text())
    rows = []
    for a in answers:
        pic = manifest.get(str(a["sheet"]), {}).get(str(a["number"]))
        if pic and a.get("decision") in ("illusion", "not_illusion", "unsure"):
            rows.append((pic, model, a["decision"], (a.get("note") or "")[:300]))
    con.executemany("INSERT OR REPLACE INTO machine_reviews (picture, model, decision, note, at) "
                    "VALUES (?, ?, ?, ?, datetime('now'))", rows)
    con.commit()
    return len(rows)


V2_COLS, V2_ROWS, V2_TILE = 4, 3, 400


def _render(s: Settings, client: PoliteClient, r) -> list[Image.Image]:
    """First frame, or up to 4 evenly spaced frames of an animation."""
    if r["mime"] == "image/svg+xml":
        return [Image.open(io.BytesIO(_svg_png(client, r["url"]))).convert("RGB")]
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(storage.disk_root(s, r["disk"]) / r["rel_path"])
    n = getattr(im, "n_frames", 1)
    frames = []
    for k in ([0] if n <= 1 else sorted({round(i * (n - 1) / 3) for i in range(4)})):
        try:
            im.seek(k)
            frames.append(im.convert("RGB"))
        except (EOFError, OSError, ValueError):          # truncated animations: keep the frames that decode
            continue
    if not frames:
        raise ValueError("no decodable frame")
    return frames


def sheets_v2(s: Settings, con: sqlite3.Connection, pictures: list[str], out_dir: Path, seed: int = 8,
              cols: int = V2_COLS, rows_: int = V2_ROWS, tile: int = V2_TILE, shuffle: bool = True) -> int:
    """Numbered contact sheets; v3 (2026-10-08) uses fewer, larger tiles (3x2 of 520px)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    q = """SELECT p.picture, i.disk, i.rel_path, i.mime,
                  (SELECT it.file_url FROM items it WHERE it.image_sha256 = p.rep_sha LIMIT 1) url
           FROM pictures p JOIN images i ON i.sha256 = p.rep_sha WHERE p.picture = ?"""
    if shuffle:
        rows = [r for r in (con.execute(q, (p,)).fetchone() for p in pictures) if r]
        random.Random(seed).shuffle(rows)
    else:                                                # keep the order; None = blank tile (keeps groups on one sheet)
        rows = [con.execute(q, (p,)).fetchone() if p else None for p in pictures]
    client = PoliteClient(s.user_agent, {"upload.wikimedia.org": 1.0})
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    per = cols * rows_
    manifest = {}
    for k in range(0, len(rows), per):
        sheet_no = k // per + 1
        sheet = Image.new("RGB", (cols * tile, rows_ * tile), "white")
        draw = ImageDraw.Draw(sheet)
        manifest[sheet_no] = {}
        for j, r in enumerate(rows[k:k + per]):
            number = j + 1
            x, y = (j % cols) * tile, (j // cols) * tile
            if r is None:
                continue
            try:
                frames = _render(s, client, r)
                if len(frames) == 1:
                    im = frames[0]
                    im.thumbnail((tile - 10, tile - 10))
                    sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
                else:                                    # animation: frames in a 2x2 grid, marked ANIM
                    half = (tile - 14) // 2
                    for f, im in enumerate(frames[:4]):
                        im.thumbnail((half, half))
                        fx, fy = x + 5 + (f % 2) * (half + 4), y + 5 + (f // 2) * (half + 4)
                        sheet.paste(im, (fx + (half - im.width) // 2, fy + (half - im.height) // 2))
                    draw.rectangle([x + tile - 96, y + 2, x + tile - 3, y + 38], fill="#1565c0")
                    draw.text((x + tile - 90, y + 4), "ANIM", fill="white", font=font)
            except Exception as e:
                log.warning("sheet %d #%d: %s", sheet_no, number, e)
                draw.text((x + 40, y + tile // 2), "(cannot render)", fill="red", font=font)
            draw.rectangle([x, y, x + tile - 1, y + tile - 1], outline="#888", width=2)
            draw.rectangle([x + 2, y + 2, x + 58, y + 40], fill="black")
            draw.text((x + 8, y + 4), str(number), fill="yellow", font=font)
            manifest[sheet_no][number] = r["picture"]
        sheet.save(out_dir / f"sheet_{sheet_no:04d}.png")
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    log.info("v2 sheets: %d sheets, %d pictures -> %s", len(manifest), len(rows), out_dir)
    return len(manifest)


def store_v2(con: sqlite3.Connection, out_dir: Path, answers: list[dict], model: str) -> int:
    """answers: [{"sheet": 1, "number": 3, "decision": "not_illusion", "trick": true, "note": "..."}]"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(machine_reviews)")}
    if "tag" not in cols:
        con.execute("ALTER TABLE machine_reviews ADD COLUMN tag TEXT")      # 'trick' = trick art / trompe-l'oeil
    manifest = json.loads((out_dir / "manifest.json").read_text())
    rows = []
    for a in answers:
        pic = manifest.get(str(a["sheet"]), {}).get(str(a["number"]))
        if pic and a.get("decision") in ("illusion", "not_illusion", "unsure"):
            rows.append((pic, model, a["decision"], (a.get("note") or "")[:300], "trick" if a.get("trick") else None))
    con.executemany("INSERT OR REPLACE INTO machine_reviews (picture, model, decision, note, tag, at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))", rows)
    con.commit()
    return len(rows)
