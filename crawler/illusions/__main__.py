"""Command line: python -m illusions <command>"""
import argparse
import logging
import sys
from datetime import datetime

from . import arai, bach, commons, hfdatasets, openverse, review, smallsites, config, db, gutenberg, illusionsindex, joi, kitaoka, ntt, sakkon, sugihara, similar, wordpress, storage, visiome, webcrawl, wikidata

HTML_SITES = {"kitaoka": kitaoka.Kitaoka, "bach": bach.Bach, "sakkon": sakkon.Sakkon,
              "illusionsindex": illusionsindex.IllusionsIndex, "ntt": ntt.IllusionForum,
              "arai": arai.AraiMuseum, "sugihara": sugihara.Sugihara,
              "novick": smallsites.NovickColorIllusions, "panoptikum": smallsites.Panoptikum,
              "eyetricks": smallsites.EyeTricks, "benricho": smallsites.Benricho, "izukan": smallsites.IZukan,
              "scholarpedia": smallsites.Scholarpedia, "exploratorium": smallsites.ExploratoriumAnnex,
              "wikidebrouillard": smallsites.Wikidebrouillard}
WP_SITES = {"moillusions": wordpress.MightyOpticalIllusions, "illusionen": wordpress.Illusionen,
            "archimedeslab": wordpress.ArchimedesLab, "anstis": wordpress.AnstisLab,
            "shapiro": wordpress.IllusionScience, "ioty": wordpress.IllusionOfTheYear,
            "opticalillusionnet": wordpress.OpticalIllusionNet, "illusionworks": wordpress.IllusionWorks,
            "kodaka": wordpress.KodakaLab, "illusionvqa": hfdatasets.IllusionVQA, "viperlib": hfdatasets.Viperlib}


def setup_logging(s: config.Settings) -> None:
    s.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(s.log_dir / f"crawl-{datetime.now():%Y%m%d}.log", encoding="utf-8")],
    )


def status(con) -> None:
    print("== crawl_categories")
    for r in con.execute("SELECT state, count(*) n, sum(n_files) f FROM crawl_categories GROUP BY state"):
        print(f"  {r['state']:10} {r['n']:6} categories  {r['f'] or 0:8} file links")
    print("== items")
    for r in con.execute("SELECT source, status, count(*) n FROM items GROUP BY source, status ORDER BY source, n DESC"):
        print(f"  {r['source']:8} {r['status']:13} {r['n']:8}")
    r = con.execute("SELECT count(*) n, coalesce(sum(bytes), 0) b, sum(mirrored_at IS NOT NULL) m, "
                    "sum(frames > 1) a FROM images").fetchone()
    r3 = con.execute("SELECT count(*) n, sum(label_ja IS NOT NULL) ja, "
                     "(SELECT count(DISTINCT item_id) FROM item_illusions) linked FROM illusions").fetchone()
    print(f"== illusions: {r3['n']} concepts ({r3['ja'] or 0} with Japanese name), {r3['linked']} images linked")
    print("== flags")
    for r2 in con.execute("SELECT i.source, f.flag, count(*) n FROM item_flags f JOIN items i ON i.id = f.item_id "
                          "GROUP BY i.source, f.flag"):
        print(f"  {r2['source']:8} {r2['flag']:18} {r2['n']:8}")
    print(f"== images: {r['n']} files, {r['b'] / 1024**3:.2f} GB, mirrored {r['m'] or 0}, animated {r['a'] or 0}")


def main() -> None:
    p = argparse.ArgumentParser(prog="illusions")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show progress")
    sub.add_parser("categories", help="Commons: walk category tree and list files")
    sub.add_parser("metadata", help="Commons: fetch file metadata")
    d = sub.add_parser("download", help="Commons: download files to the HDD pair")
    d.add_argument("--limit", type=int)
    d.add_argument("--minutes", type=float, help="stop after this many minutes")
    sub.add_parser("mirror", help="copy primaries to mirrors, verify, back up DB")
    sub.add_parser("nas", help="third copy: sync images and DB snapshots to the NAS")
    v = sub.add_parser("verify", help="check stored files on both disks")
    v.add_argument("--full", action="store_true", help="re-hash every file")
    v.add_argument("--repair", action="store_true", help="restore bad copies from the other disk, re-queue the rest")
    v.add_argument("--since", help="only images stored after this time, e.g. '2026-10-05 01:00:00' (UTC)")
    r = sub.add_parser("commons", help="run all Commons steps, then mirror")
    r.add_argument("--limit", type=int, help="max downloads this run")
    r.add_argument("--minutes", type=float, help="stop downloading after this many minutes")
    sub.add_parser("exclude", help="Commons: re-apply sources/commons_exclude.txt to crawled data")
    sub.add_parser("visiome", help="Visiome: list stimulus files, download, then mirror")
    sub.add_parser("gutenberg", help="Project Gutenberg: figures of public-domain books, then mirror")
    for name in HTML_SITES:
        h = sub.add_parser(name, help=f"HTML site {name}: crawl pages, download images, then mirror")
        h.add_argument("--minutes", type=float, help="stop after this many minutes (crawl + download)")
        h.add_argument("--refresh", action="store_true",
                       help="re-read all crawled pages and update metadata of known images")
        h.add_argument("--reextract", action="store_true",
                       help="redo extraction from cached pages only (offline), then stop")
    sub.add_parser("similar", help="perceptual hashes, near-duplicate pairs, and the flags derived from them")
    sub.add_parser("review-prepare", help="group files into pictures and build the human review queues")
    sub.add_parser("v2-check-prepare", help="draw the blind 300-picture human check of Claude's v2 judgements")
    sub.add_parser("tax2-check-prepare", help="draw the blind human check of Claude's main categories")
    sub.add_parser("labels", help="final label per picture (v3 over v2) and the small-image cut (320x240)")
    sub.add_parser("taxonomy-prepare", help="initial illusion categories and the category review queues")
    rv = sub.add_parser("review-serve", help="LAN web app for human review (institute network + token only)")
    rv.add_argument("--port", type=int, default=8765)
    o = sub.add_parser("openverse", help="Openverse API (CC images, Wikimedia excluded), then download and mirror")
    o.add_argument("--minutes", type=float, default=100)
    j = sub.add_parser("joi", help="Journal of Illusion (Crawl-delay 60 s): article figures, in chunks")
    j.add_argument("--minutes", type=float, default=100)
    for name in WP_SITES:
        w = sub.add_parser(name, help=f"WordPress site {name}: list posts+media via REST API, download, mirror")
        w.add_argument("--minutes", type=float, help="stop downloading after this many minutes")
    sub.add_parser("wikidata", help="build the illusion name dictionary (Wikidata + Wikipedia) and link images")
    sub.add_parser("link", help="re-link stored images to illusion concepts (offline)")
    args = p.parse_args()

    s = config.load()
    setup_logging(s)
    con = db.connect(s.db_path)

    needs_network = args.cmd in ("categories", "metadata", "download", "commons", "visiome", "gutenberg", "wikidata", "joi", "openverse", *HTML_SITES, *WP_SITES)
    if needs_network and not s.contact:
        sys.exit("settings.toml: set 'contact' before crawling (Wikimedia User-Agent policy)")

    if args.cmd == "status":
        status(con)
    elif args.cmd == "mirror":
        storage.mirror(s, con)
    elif args.cmd == "nas":
        storage.nas_sync(s, con)
        r = con.execute("SELECT count(*), sum(nas_at IS NOT NULL) FROM images").fetchone()
        print(f"NAS copies: {r[1] or 0}/{r[0]}")
    elif args.cmd == "review-prepare":
        review.prepare(con)
    elif args.cmd == "v2-check-prepare":
        from . import review_v2
        review_v2.prepare(con)
    elif args.cmd == "tax2-check-prepare":
        from . import review_tax2
        review_tax2.prepare(con)
    elif args.cmd == "labels":
        from . import labels
        labels.build(con)
    elif args.cmd == "taxonomy-prepare":
        from . import taxonomy
        taxonomy.prepare(con)
    elif args.cmd == "review-serve":
        review.serve(s, s.db_path, port=args.port)
    elif args.cmd == "openverse":
        site = openverse.Openverse()
        storage.active_pair(s)
        client = webcrawl.make_client(s, site)
        start = datetime.now()
        site.collect(s, con, client, minutes=args.minutes * 0.3)
        left = args.minutes - (datetime.now() - start).total_seconds() / 60
        webcrawl.download(s, con, client, site, minutes=max(left, 1))
        storage.mirror(s, con)
        status(con)
    elif args.cmd == "joi":
        site = joi.JournalOfIllusion()
        storage.active_pair(s)
        client = webcrawl.make_client(s, site)
        start = datetime.now()
        site.collect(s, con, client, minutes=args.minutes * 0.5)        # half the time for pages,
        site.collect_pdfs(s, con, client, minutes=args.minutes * 0.3)  # PDF-only articles,
        left = args.minutes - (datetime.now() - start).total_seconds() / 60
        webcrawl.download(s, con, client, site, minutes=max(left, 1))   # the rest for figures
        storage.mirror(s, con)
        status(con)
    elif args.cmd in WP_SITES:
        site = WP_SITES[args.cmd]()
        storage.active_pair(s)
        client = webcrawl.make_client(s, site)
        if not con.execute("SELECT 1 FROM items WHERE source = ? LIMIT 1", (site.source,)).fetchone():
            site.collect(s, con, client)           # listing is done once; later runs only download
        webcrawl.download(s, con, client, site, minutes=args.minutes)
        storage.mirror(s, con)
        status(con)
    elif args.cmd in HTML_SITES:
        site = HTML_SITES[args.cmd]()
        if args.reextract:
            webcrawl.reextract(s, con, site)
            return
        storage.active_pair(s)                # fail early if disks are not mounted
        client = webcrawl.make_client(s, site)
        start = datetime.now()
        webcrawl.crawl(s, con, client, site, minutes=args.minutes, refresh=args.refresh)
        left = args.minutes - (datetime.now() - start).total_seconds() / 60 if args.minutes else None
        if left is None or left > 1:
            webcrawl.download(s, con, client, site, minutes=left)
        storage.mirror(s, con)
        status(con)
    elif args.cmd == "similar":
        similar.compute_hashes(s, con)
        similar.find_matches(con)
        similar.apply_flags(s, con)
        status(con)
    elif args.cmd in ("wikidata", "link"):
        if args.cmd == "wikidata":
            client = wikidata.make_client(s)
            wikidata.build_dictionary(s, con, client)
            wikidata.classify_kinds(con, client)
        wikidata.link_items(con)
        status(con)
    elif args.cmd == "exclude":
        commons.apply_exclusions(con)
        status(con)
    elif args.cmd in ("visiome", "gutenberg"):
        module = visiome if args.cmd == "visiome" else gutenberg
        storage.active_pair(s)                # fail early if disks are not mounted
        client = module.make_client(s)
        module.list_items(s, con, client)
        module.download(s, con, client)
        storage.mirror(s, con)
        status(con)
    elif args.cmd == "verify":
        n = storage.verify(s, con, full=args.full, repair=args.repair, since=args.since)
        print("OK" if n == 0 else f"{n} problems (see log)")
        sys.exit(1 if n else 0)
    else:
        client = commons.make_client(s)
        if args.cmd in ("categories", "commons"):
            commons.crawl_categories(s, con, client)
        if args.cmd in ("metadata", "commons"):
            commons.fetch_metadata(s, con, client)
        if args.cmd in ("download", "commons"):
            storage.active_pair(s)            # fail early if disks are not mounted
            commons.download(s, con, client, limit=args.limit, minutes=args.minutes)
        if args.cmd == "commons":
            storage.mirror(s, con)
        status(con)


if __name__ == "__main__":
    main()
