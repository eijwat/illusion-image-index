"""Re-render the tiles the visual check could not see (black / blank), compositing transparency onto white.
    python3 dbpaper/rerender.py <pictures.txt> <out dir>"""
import io, json, sqlite3, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from illusions import config, storage
from illusions.client import PoliteClient
from illusions.machine_review import _svg_png

def flat(im):
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, "white")
    return Image.alpha_composite(bg, im).convert("RGB")

def main():
    pics = Path(sys.argv[1]).read_text().split()
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    s = config.load()
    con = sqlite3.connect(f"file:{ROOT/'work'/'illusions.db'}?mode=ro", uri=True)
    client = PoliteClient(s.user_agent, {"upload.wikimedia.org": 1.0})
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
    Image.MAX_IMAGE_PIXELS = None
    cols, rows, tile = 3, 2, 520
    manifest = {}
    for k in range(0, len(pics), cols * rows):
        no = k // (cols * rows) + 1
        sheet = Image.new("RGB", (cols * tile, rows * tile), "white"); d = ImageDraw.Draw(sheet)
        manifest[no] = {}
        for j, p in enumerate(pics[k:k + cols * rows]):
            x, y = (j % cols) * tile, (j // cols) * tile
            disk, rel, mime, url = con.execute("""SELECT i.disk, i.rel_path, i.mime,
                (SELECT it.file_url FROM items it WHERE it.image_sha256 = p.rep_sha AND it.file_url LIKE '%wikimedia%' LIMIT 1)
                FROM pictures p JOIN images i ON i.sha256 = p.rep_sha WHERE p.picture = ?""", (p,)).fetchone()
            try:
                if mime == "image/svg+xml":
                    if not url:
                        raise ValueError("SVG without a Commons URL")
                    im = Image.open(io.BytesIO(_svg_png(client, url)))
                else:
                    im = Image.open(storage.disk_root(s, disk) / rel)
                im = flat(im)
                im.thumbnail((tile - 10, tile - 10))
                sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
            except Exception as e:
                d.text((x + 20, y + tile // 2), f"(cannot render: {str(e)[:25]})", fill="red", font=font)
            d.rectangle([x, y, x + tile - 1, y + tile - 1], outline="#888", width=2)
            d.rectangle([x + 2, y + 2, x + 58, y + 40], fill="black")
            d.text((x + 8, y + 4), str(j + 1), fill="yellow", font=font)
            manifest[no][j + 1] = p
        sheet.save(out / f"sheet_{no:04d}.png")
    (out / "manifest.json").write_text(json.dumps(manifest))
    print(len(manifest), "sheets")

main()
