"""Illusion name dictionary from Wikidata + Wikipedia, and linking of stored images to it.

Which concepts count as illusions comes from two places, because Wikidata's class tree misses
many famous illusions (Kanizsa triangle, Necker cube...) and also drags in non-illusions
(special effects, sound effects):
  1. Wikidata: everything under 'optical illusion' (Q174923), minus sources/wikidata_exclude.txt
  2. en Wikipedia 'List of optical illusions': the article linked in each table row's Name cell
For each concept we keep multilingual labels, discoverer, date, Commons category / image,
and the intro of the en/ja Wikipedia articles. Images are then linked to concepts through
the Commons file (P18 / the list's example image) and Commons categories.
"""
import json
import logging
import re
import sqlite3

from . import commons
from .client import PoliteClient
from .config import PROJECT_ROOT, Settings

log = logging.getLogger(__name__)

ROOT = "Q174923"                      # optical illusion
SPARQL = "https://query.wikidata.org/sparql"
WD_API = "https://www.wikidata.org/w/api.php"
WIKI_API = "https://{}.wikipedia.org/w/api.php"
LIST_PAGE = "List of optical illusions"
EXCLUDE_FILE = PROJECT_ROOT / "sources" / "wikidata_exclude.txt"


def make_client(s: Settings) -> PoliteClient:
    return PoliteClient(s.user_agent, {
        "query.wikidata.org": 2.0,
        "www.wikidata.org": s.api_interval,
        "en.wikipedia.org": s.api_interval,
        "ja.wikipedia.org": s.api_interval,
    })


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- 1. which concepts

def class_members(client: PoliteClient) -> set[str]:
    bad = [l.split("#")[0].strip() for l in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines()]
    values = " ".join(f"wd:{q}" for q in bad if q)
    query = f"""SELECT DISTINCT ?i WHERE {{
      ?i (wdt:P31|wdt:P279)/wdt:P279* wd:{ROOT} .
      FILTER NOT EXISTS {{ VALUES ?bad {{ {values} }} ?i (wdt:P31|wdt:P279)* ?bad . }}
    }}"""
    resp = client.get(SPARQL, params={"query": query, "format": "json"})
    resp.raise_for_status()
    return {b["i"]["value"].rsplit("/", 1)[-1] for b in resp.json()["results"]["bindings"]}


def list_page_rows(client: PoliteClient) -> list[dict]:
    """Rows of the en 'List of optical illusions' table: linked article, example file, notes."""
    data = commons.api(client, _url=WIKI_API.format("en"), action="parse", page=LIST_PAGE, prop="wikitext")
    wikitext = data["parse"]["wikitext"]
    rows = []
    for block in wikitext.split("\n|-")[1:]:
        cells = [c[1:].strip() for c in block.strip().split("\n") if c.startswith("|") and not c.startswith("|}")]
        if len(cells) < 2:
            continue
        link = re.search(r"\[\[([^\]|#]+)", cells[0])
        file = re.search(r"\[\[(?:File|Image):([^\]|]+)", cells[1])
        if link:
            rows.append({"article": link.group(1).strip(),
                         "file": file.group(1).strip() if file else None,
                         "notes": re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]|'''?", r"\1", cells[2]) if len(cells) > 2 else None})
    return rows


def resolve_articles(client: PoliteClient, titles: list[str]) -> dict[str, str]:
    """en Wikipedia title -> Wikidata QID (following redirects)."""
    out = {}
    for batch in _chunks(titles, 50):
        data = commons.api(client, _url=WIKI_API.format("en"), titles="|".join(batch),
                           prop="pageprops", ppprop="wikibase_item", redirects="1")
        q = data["query"]
        back = {}                                   # final title -> requested title
        for n in q.get("normalized", []):
            back[n["to"]] = n["from"]
        for r in q.get("redirects", []):
            back[r["to"]] = back.get(r["from"], r["from"])
        for page in q["pages"]:
            qid = page.get("pageprops", {}).get("wikibase_item")
            if qid:
                out[back.get(page["title"], page["title"])] = qid
    return out


# ---------------------------------------------------------------- 2. details

def _claim_values(entity: dict, prop: str) -> list:
    vals = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v is not None:
            vals.append(v)
    return vals


def _label(entity: dict, lang: str, kind: str = "labels") -> str | None:
    return entity.get(kind, {}).get(lang, {}).get("value")


def fetch_entities(client: PoliteClient, qids: list[str], props: str) -> dict[str, dict]:
    out = {}
    for batch in _chunks(qids, 50):
        data = commons.api(client, _url=WD_API, action="wbgetentities", ids="|".join(batch), props=props)
        out.update({k: v for k, v in data.get("entities", {}).items() if "missing" not in v})
    return out


def fetch_extracts(client: PoliteClient, lang: str, titles: list[str]) -> dict[str, str]:
    out = {}
    for batch in _chunks(titles, 20):               # exintro allows at most 20 pages per request
        data = commons.api(client, _url=WIKI_API.format(lang), titles="|".join(batch), prop="extracts",
                           exintro="1", explaintext="1", exlimit="20", redirects="1")
        q = data["query"]
        back = {r["to"]: r["from"] for r in q.get("redirects", [])}
        for n in q.get("normalized", []):
            back[n["to"]] = n["from"]
        for page in q["pages"]:
            if page.get("extract"):
                out[back.get(page["title"], page["title"])] = page["extract"].strip()
    return out


EN_CATEGORY = "Category:Optical illusions"
NOT_CONCEPTS = {"Q5", "Q4167836", "Q13406463", "Q4167410", "Q11266439"}   # human, category, list, disambiguation, template
EXTRACT_LANGS = ["en", "ja", "de", "fr", "es", "ru", "zh", "it", "ko", "pt", "nl"]


def language_categories(client: PoliteClient) -> dict[str, str]:
    """Every Wikipedia edition's counterpart of en 'Category:Optical illusions', via its Wikidata item."""
    data = commons.api(client, _url=WIKI_API.format("en"), titles=EN_CATEGORY, prop="pageprops", ppprop="wikibase_item")
    qid = data["query"]["pages"][0]["pageprops"]["wikibase_item"]
    links = fetch_entities(client, [qid], "sitelinks")[qid]["sitelinks"]
    return {site[:-4].replace("_", "-"): v["title"] for site, v in links.items()
            if site.endswith("wiki") and site not in ("commonswiki", "specieswiki", "metawiki", "wikidatawiki", "mediawikiwiki")}


def category_concepts(client: PoliteClient) -> dict[str, set[str]]:
    """QID -> set of languages whose illusion category (or a direct subcategory) contains its article."""
    found: dict[str, set[str]] = {}
    for lang, cat in sorted(language_categories(client).items()):
        api_url = WIKI_API.format(lang)
        titles, cats, seen = [], [cat], set()
        for depth in range(2):                                  # the category and its direct subcategories
            nxt = []
            for c in cats:
                if c in seen:
                    continue
                seen.add(c)
                params = {"list": "categorymembers", "cmtitle": c, "cmtype": "page|subcat", "cmlimit": "500"}
                while True:
                    d = commons.api(client, _url=api_url, **params)
                    for m in d["query"]["categorymembers"]:
                        if m["ns"] == 0:
                            titles.append(m["title"])
                        elif m["ns"] == 14:
                            nxt.append(m["title"])
                    if "continue" not in d:
                        break
                    params.update(d["continue"])
            cats = nxt if depth == 0 else []
        for batch in _chunks(sorted(set(titles)), 50):
            d = commons.api(client, _url=api_url, titles="|".join(batch), prop="pageprops", ppprop="wikibase_item")
            for page in d["query"]["pages"]:
                if q := page.get("pageprops", {}).get("wikibase_item"):
                    found.setdefault(q, set()).add(lang)
        log.info("wikidata: %s %s -> %d articles", lang, cat, len(set(titles)))
    return found


def build_dictionary(s: Settings, con: sqlite3.Connection, client: PoliteClient) -> None:
    for col in ("extracts_json TEXT", "n_languages INTEGER"):
        if col.split()[0] not in [c[1] for c in con.execute("PRAGMA table_info(illusions)")]:
            con.execute(f"ALTER TABLE illusions ADD COLUMN {col}")
    con.commit()
    by_category = category_concepts(client)
    from_class = class_members(client)
    rows = list_page_rows(client)
    resolved = resolve_articles(client, [r["article"] for r in rows])
    from_list = set(resolved.values())
    log.info("wikidata: %d concepts from class tree, %d from the en list (%d rows), %d in both",
             len(from_class), len(from_list), len(rows), len(from_class & from_list))

    entities = fetch_entities(client, sorted(from_class | from_list | set(by_category)),
                              "labels|aliases|descriptions|claims|sitelinks")
    # category members can be people (artists, scientists) or lists: keep only concepts
    for q in [q for q in entities if q not in from_class | from_list]:
        kinds = {v.get("id") for v in _claim_values(entities[q], "P31")}
        if kinds & NOT_CONCEPTS or not kinds:
            del entities[q]
    from_cats = set(entities) - from_class - from_list
    log.info("wikidata: %d more concepts from %d language categories", len(from_cats),
             len({l for ls in by_category.values() for l in ls}))
    discoverer_ids = sorted({v["id"] for e in entities.values() for v in _claim_values(e, "P61")})
    names = fetch_entities(client, discoverer_ids, "labels") if discoverer_ids else {}

    titles = {lang: {q: e.get("sitelinks", {}).get(f"{lang}wiki", {}).get("title") for q, e in entities.items()}
              for lang in EXTRACT_LANGS}
    extracts = {lang: fetch_extracts(client, lang, sorted(t for t in titles[lang].values() if t))
                for lang in EXTRACT_LANGS}

    for qid, e in entities.items():
        p18 = _claim_values(e, "P18")
        p373 = _claim_values(e, "P373")
        p571 = _claim_values(e, "P571")
        discoverers = [_label(names.get(v["id"], {}), "en") or v["id"] for v in _claim_values(e, "P61")]
        via = "+".join(n for n, src in (("wikidata_class", from_class), ("enwiki_list", from_list),
                                        ("wiki_categories", set(by_category))) if qid in src)
        langs = by_category.get(qid, set())
        ex = {lang: extracts[lang].get(titles[lang].get(qid)) for lang in EXTRACT_LANGS}
        ex = {k: v for k, v in ex.items() if v}
        slim = {k: e.get(k) for k in ("labels", "aliases", "descriptions", "sitelinks")}
        con.execute(
            """INSERT INTO illusions (qid, label_en, label_ja, description_en, description_ja, discoverer, inception,
                   commons_category, image_file, extract_en, extract_ja, found_via, entity_json,
                   extracts_json, n_languages, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(qid) DO UPDATE SET label_en = excluded.label_en, label_ja = excluded.label_ja,
                   description_en = excluded.description_en, description_ja = excluded.description_ja,
                   discoverer = excluded.discoverer, inception = excluded.inception,
                   commons_category = excluded.commons_category, image_file = excluded.image_file,
                   extract_en = excluded.extract_en, extract_ja = excluded.extract_ja,
                   found_via = excluded.found_via, entity_json = excluded.entity_json,
                   extracts_json = excluded.extracts_json, n_languages = excluded.n_languages,
                   updated_at = excluded.updated_at""",
            (qid, _label(e, "en"), _label(e, "ja"), _label(e, "en", "descriptions"), _label(e, "ja", "descriptions"),
             ", ".join(discoverers) or None,
             p571[0]["time"].lstrip("+").split("-")[0] if p571 else None,
             p373[0] if p373 else None, p18[0] if p18 else None,
             extracts["en"].get(titles["en"].get(qid)), extracts["ja"].get(titles["ja"].get(qid)),
             via, json.dumps(slim, ensure_ascii=False), json.dumps(ex, ensure_ascii=False), len(langs) or None))
    con.commit()

    # the list's example images are also evidence for linking
    con.executemany("UPDATE illusions SET list_example_file = ? WHERE qid = ?",
                    [(r["file"], resolved[r["article"]]) for r in rows if r["file"] and r["article"] in resolved])
    con.commit()
    log.info("wikidata: %d illusion concepts stored (%d with ja label, %d with en extract, %d with ja extract)",
             len(entities), sum(1 for e in entities.values() if _label(e, "ja")),
             len(extracts["en"]), len(extracts["ja"]))


# ---------------------------------------------------------------- 3. link images

def _norm_file(name: str) -> str:
    name = name.replace("_", " ").strip()
    return "File:" + name[0].upper() + name[1:] if name else name


def link_items(con: sqlite3.Connection) -> None:
    """Link Commons items to illusion concepts. Rebuilt from scratch each time (it is derived data)."""
    con.execute("DELETE FROM item_illusions")
    file_ids = dict(con.execute("SELECT source_id, id FROM items WHERE source = 'commons' AND status = 'stored'").fetchall())
    stored = set(file_ids.values())
    # A crawl seed (depth 0, e.g. 'Optical illusions') is the whole collection, never one illusion;
    # Wikidata sometimes gives it as a concept's P373 (seen on Fraser spiral illusion, 2026-10-03).
    seeds = {r[0] for r in con.execute("SELECT title FROM crawl_categories WHERE source = 'commons' AND depth = 0")}

    def add(item_id, qid, method):
        if item_id not in stored:
            return
        con.execute("INSERT OR IGNORE INTO item_illusions (item_id, qid, method) VALUES (?, ?, ?)",
                    (item_id, qid, method))

    concepts = con.execute("SELECT qid, image_file, list_example_file, commons_category FROM illusions").fetchall()
    for c in concepts:                                  # 1. images that represent the concept
        if c["image_file"] and (i := file_ids.get(_norm_file(c["image_file"]))):
            add(i, c["qid"], "p18")
        if c["list_example_file"] and (i := file_ids.get(_norm_file(c["list_example_file"]))):
            add(i, c["qid"], "enwiki_list_example")

    # 2. Commons categories: the concept's category, then its subcategories found by the crawl
    parent = dict(con.execute("SELECT title, parent FROM crawl_categories WHERE source = 'commons'").fetchall())
    children: dict[str, list[str]] = {}
    for t, p in parent.items():
        if p:
            children.setdefault(p, []).append(t)
    for c in concepts:
        if not c["commons_category"]:
            continue
        root = "Category:" + c["commons_category"]
        if root in seeds:
            log.warning("%s: Commons category %s is a crawl seed; not used for linking", c["qid"], root)
            continue
        stack, seen = [(root, "commons_category")], set()
        while stack:
            cat, method = stack.pop()
            if cat in seen:
                continue
            seen.add(cat)
            for (item_id,) in con.execute("SELECT item_id FROM item_categories WHERE category = ?", (cat,)):
                add(item_id, c["qid"], method)
            stack.extend((ch, "commons_subcategory") for ch in children.get(cat, []))
    con.commit()
    _link_by_name(con, stored)
    _link_by_appearance(con)
    n_items = con.execute("SELECT count(DISTINCT item_id) FROM item_illusions").fetchone()[0]
    n_concepts = con.execute("SELECT count(DISTINCT qid) FROM item_illusions").fetchone()[0]
    log.info("linked %d images to %d illusion concepts", n_items, n_concepts)


# ---------------------------------------------------------------- 4. kinds

# What a concept is, from Wikidata's class tree (first match wins). Anything else is a 'phenomenon'.
KIND_ROOTS = [
    ("artwork", ["Q838948", "Q4502142", "Q3305213", "Q11060274", "Q18573970"]),   # work of art, visual artwork, painting, print, lithograph
    ("place", ["Q33506", "Q41176", "Q811979", "Q2221906", "Q570116", "Q17350442"]),  # museum, building, structure, location, attraction, venue
    ("organization", ["Q43229", "Q431289", "Q4830453"]),                            # organization, brand, business
    ("fictional", ["Q14897293", "Q24334685", "Q11002"]),                            # fictional entity, legendary entity, ship? (ghost ships)
    ("device", ["Q1183543", "Q39546", "Q11019", "Q2695280"]),                       # device, tool, machine, technique
    ("disorder", ["Q12136", "Q929833"]),                                            # disease, rare disease
]


def classify_kinds(con: sqlite3.Connection, client: PoliteClient) -> None:
    if "kind" not in [c[1] for c in con.execute("PRAGMA table_info(illusions)")]:
        con.execute("ALTER TABLE illusions ADD COLUMN kind TEXT")
        con.commit()
    qids = [r[0] for r in con.execute("SELECT qid FROM illusions")]
    kinds: dict[str, str] = {}
    for kind, roots in KIND_ROOTS:
        todo = [q for q in qids if q not in kinds]
        for batch in _chunks(todo, 150):
            query = f"""SELECT DISTINCT ?i WHERE {{
              VALUES ?i {{ {" ".join("wd:" + q for q in batch)} }}
              VALUES ?root {{ {" ".join("wd:" + r for r in roots)} }}
              ?i wdt:P31/wdt:P279* ?root .
            }}"""
            resp = client.get(SPARQL, params={"query": query, "format": "json"})
            resp.raise_for_status()
            for b in resp.json()["results"]["bindings"]:
                kinds[b["i"]["value"].rsplit("/", 1)[-1]] = kind
    con.executemany("UPDATE illusions SET kind = ? WHERE qid = ?",
                    [(kinds.get(q, "phenomenon"), q) for q in qids])
    # 'core': curated sources (Wikidata class tree, en list) or listed by >= 3 language categories.
    # Category-only concepts from 1-2 languages are candidates (they include magic tricks, films, places).
    if "core" not in [c[1] for c in con.execute("PRAGMA table_info(illusions)")]:
        con.execute("ALTER TABLE illusions ADD COLUMN core INTEGER")
    con.execute("UPDATE illusions SET core = CASE WHEN found_via LIKE '%wikidata_class%' OR found_via LIKE '%enwiki_list%' "
                "OR coalesce(n_languages, 0) >= 3 THEN 1 ELSE 0 END")
    con.commit()
    counts = {}
    for q in qids:
        counts[kinds.get(q, "phenomenon")] = counts.get(kinds.get(q, "phenomenon"), 0) + 1
    log.info("wikidata: kinds %s", counts)


# ---------------------------------------------------------------- linking beyond Commons

NAME_LANGS = ("en", "ja", "de", "fr", "es", "it")
# Concepts on the en list that are not illusions themselves; their names match too much ordinary text.
NO_NAME_MATCH = {
    "Q43261",   # horizon (the en list links it from a row about the horizon; 'horizontal' etc. matched everywhere)
}
GENERIC_NAMES = {"illusion", "illusions", "optical illusion", "optical illusions", "visual illusion", "錯視", "錯覚",
                 "目の錯覚", "optische täuschung", "illusion d'optique", "ilusión óptica", "illusione ottica",
                 "afterimage illusion", "art", "animation", "artifact", "moon", "illusionism"}


def _names(con: sqlite3.Connection) -> dict[str, set[str]]:
    """name (lower case) -> concept QIDs, from labels and aliases of core concepts in a few languages."""
    names: dict[str, set[str]] = {}
    for qid, entity_json in con.execute("SELECT qid, entity_json FROM illusions WHERE core = 1 AND kind = 'phenomenon'"):
        e = json.loads(entity_json or "{}")
        if qid in NO_NAME_MATCH:
            continue
        cands = [v.get("value") for lang, v in (e.get("labels") or {}).items() if lang in NAME_LANGS]
        # aliases only in en/ja: other languages' aliases collide with English words (de 'Horizontal')
        cands += [a.get("value") for lang, al in (e.get("aliases") or {}).items() if lang in ("en", "ja") for a in al]
        for n in cands:
            n = (n or "").strip().lower()
            cjk = bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", n))
            if not n or n in GENERIC_NAMES or len(n) < (3 if cjk else 5):
                continue
            names.setdefault(n, set()).add(qid)
    return names


def _link_by_name(con: sqlite3.Connection, stored: set[int]) -> None:
    names = _names(con)
    latin = [n for n in names if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", n)]
    cjk = [n for n in names if n not in latin]
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(latin, key=len, reverse=True)) + r")\b"
                         + ("|(" + "|".join(re.escape(n) for n in sorted(cjk, key=len, reverse=True)) + ")" if cjk else ""))
    rows = con.execute("SELECT id, source, title, substr(description, 1, 400) d FROM items "
                       "WHERE status = 'stored' AND source != 'commons'").fetchall()
    links = set()
    for r in rows:
        text = f"{r['title'] or ''} {r['d'] or ''}".lower()
        for m in pattern.finditer(text):
            for q in names[m.group(0)]:
                links.add((r["id"], q))
    con.executemany("INSERT OR IGNORE INTO item_illusions (item_id, qid, method) VALUES (?, ?, 'name_match')", links)
    con.commit()
    log.info("name match: %d links on %d images (%d names)", len(links), len({l[0] for l in links}), len(names))


def _link_by_appearance(con: sqlite3.Connection) -> None:
    """An image that looks the same as a linked image (image_matches, or the same file) shows the same illusion."""
    if not con.execute("SELECT name FROM sqlite_master WHERE name = 'image_matches'").fetchone():
        return
    by_sha: dict[str, list[int]] = {}
    for item_id, sha in con.execute("SELECT id, image_sha256 FROM items WHERE status = 'stored'"):
        by_sha.setdefault(sha, []).append(item_id)
    linked: dict[str, set[str]] = {}
    for sha, qid in con.execute("SELECT i.image_sha256, ii.qid FROM item_illusions ii JOIN items i ON i.id = ii.item_id "
                                "WHERE ii.method != 'appearance'"):
        linked.setdefault(sha, set()).add(qid)
    pairs = [(a, b) for a, b in con.execute("SELECT sha_a, sha_b FROM image_matches")]
    pairs += [(s, s) for s in by_sha]                     # the same file under several items
    links = set()
    for a, b in pairs:
        for x, y in ((a, b), (b, a)):
            for q in linked.get(x, ()):
                for item_id in by_sha.get(y, ()):
                    links.add((item_id, q))
    con.executemany("INSERT OR IGNORE INTO item_illusions (item_id, qid, method) VALUES (?, ?, 'appearance')", links)
    con.commit()
    log.info("appearance: %d links", len(links))
