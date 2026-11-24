"""Image storage on mirrored HDD pairs.

Files are written to the primary disk of the active pair and copied to its mirror
by `mirror()` (rsync), which also verifies the copies and backs up the DB.
Layout: <media_root>/<disk>/<store_subdir>/images/ab/cd/<sha256>.<ext>
"""
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    pass


def disk_root(s: Settings, label: str) -> Path:
    """Return the store dir on a disk, refusing if the disk is not actually mounted there."""
    mount = s.media_root / label
    device = Path("/dev/disk/by-label") / label
    if not device.exists():
        raise StorageError(f"disk {label} is not connected")
    if not os.path.ismount(mount):
        raise StorageError(f"disk {label} is not mounted at {mount} "
                           f"(run: udisksctl mount -b {device})")
    # Make sure the mount really is this disk, not another one that happens to use the path.
    src = subprocess.run(["findmnt", "-no", "SOURCE", str(mount)],
                         capture_output=True, text=True).stdout.strip()
    if os.path.realpath(src) != os.path.realpath(device):
        raise StorageError(f"{mount} is mounted from {src}, not from {label}")
    return mount / s.store_subdir


def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def active_pair(s: Settings) -> tuple[str, str]:
    """First pair whose primary still has more than reserve_gb free."""
    reserve = s.reserve_gb * 1024**3
    for primary, mirror in s.pairs:
        root = disk_root(s, primary)
        disk_root(s, mirror)
        if free_bytes(root.parent) > reserve:
            return primary, mirror
        log.info("pair %s/%s is full, trying next", primary, mirror)
    raise StorageError("all HDD pairs are full - time to decide the next step (see CLAUDE.md)")


def rel_path_for(sha256: str, ext: str) -> str:
    return f"images/{sha256[:2]}/{sha256[2:4]}/{sha256}.{ext}"


def store(s: Settings, tmp_file: Path, sha256: str, ext: str) -> tuple[str, str, str]:
    """Move a downloaded temp file into the active pair's primary disk. Returns (disk, mirror, rel_path)."""
    primary, mirror = active_pair(s)
    rel = rel_path_for(sha256, ext)
    dest = disk_root(s, primary) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        part = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(tmp_file, part)
        with open(part, "rb") as f:
            os.fsync(f.fileno())
        part.rename(dest)
    tmp_file.unlink(missing_ok=True)
    return primary, mirror, rel


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mirror(s: Settings, con: sqlite3.Connection, keep_db_snapshots: int = 7) -> None:
    """Copy each pair's primary to its mirror, verify new copies, and back up the DB to both disks."""
    for primary, mirror_label in s.pairs:
        try:
            src, dst = disk_root(s, primary), disk_root(s, mirror_label)
        except StorageError as e:
            log.info("skip pair %s/%s: %s", primary, mirror_label, e)
            continue
        if not src.exists():
            continue
        dst.mkdir(exist_ok=True)
        log.info("rsync %s -> %s", src, dst)
        # "/db/" is anchored: an unanchored "db/" would also skip images/db/ (sha256 prefix "db")
        subprocess.run(["rsync", "-a", "--exclude", "*.part", "--exclude", "/db/",
                        f"{src}/", f"{dst}/"], check=True)

        rows = con.execute("SELECT sha256, rel_path FROM images "
                           "WHERE disk = ? AND mirrored_at IS NULL", (primary,)).fetchall()
        ok = 0
        for r in rows:
            copy = dst / r["rel_path"]
            if copy.exists() and sha256_of(copy) == r["sha256"]:
                con.execute("UPDATE images SET mirrored_at = datetime('now') WHERE sha256 = ?",
                            (r["sha256"],))
                ok += 1
            else:
                log.error("mirror copy missing or corrupt: %s:%s", mirror_label, r["rel_path"])
        con.commit()
        log.info("verified %d/%d new mirror copies on %s", ok, len(rows), mirror_label)

        for root in (src, dst):
            _backup_db(con, root / "db", keep_db_snapshots)
    # The NAS copy is a slow "bonus" (gvfs over 1GbE): it runs separately (`illusions nas`), not here.


def _backup_db(con: sqlite3.Connection, db_dir: Path, keep: int) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    target = db_dir / f"illusions-{datetime.now():%Y%m%d-%H%M%S}.db"
    out = sqlite3.connect(target)
    with out:
        con.backup(out)
    out.close()
    snapshots = sorted(db_dir.glob("illusions-*.db"))
    for old in snapshots[:-keep]:
        old.unlink()
    log.info("DB backup -> %s", target)


def _check(path: Path, size: int, sha256: str, full: bool) -> str | None:
    if not path.exists():
        return "missing"
    if path.stat().st_size != size or (full and sha256_of(path) != sha256):
        return "corrupt"
    return None


def verify(s: Settings, con: sqlite3.Connection, full: bool = False, repair: bool = False,
           since: str | None = None) -> int:
    """Check every image on both disks (full=True re-hashes). Returns the number of problems left.

    repair=True fixes what it can: a bad copy is replaced from the good one on the other disk;
    if neither copy is good, the image row is dropped and its items go back to the download queue.
    since='YYYY-mm-dd HH:MM:SS' limits the check to images stored after that time (e.g. a crash).
    """
    problems = 0
    query = "SELECT sha256, bytes, disk, mirror_disk, rel_path, mirrored_at FROM images"
    rows = con.execute(query + (" WHERE created_at >= ?" if since else ""), (since,) if since else ()).fetchall()
    for r in rows:
        primary = disk_root(s, r["disk"]) / r["rel_path"]
        mirror = disk_root(s, r["mirror_disk"]) / r["rel_path"]
        bad_p = _check(primary, r["bytes"], r["sha256"], full)
        bad_m = _check(mirror, r["bytes"], r["sha256"], full) if r["mirrored_at"] else None
        if not bad_p and not bad_m:
            continue
        log.error("%s: %s:%s %s / %s:%s %s", r["sha256"][:12], r["disk"], bad_p or "ok",
                  r["mirror_disk"], bad_m or ("ok" if r["mirrored_at"] else "not mirrored yet"), r["rel_path"], "")
        if not repair:
            problems += bool(bad_p) + bool(bad_m)
            continue
        good = None
        for path in (primary, mirror):
            if path.exists() and sha256_of(path) == r["sha256"]:
                good = path
                break
        if good is None:
            # no good copy anywhere: forget the file and download it again
            for path in (primary, mirror):
                path.unlink(missing_ok=True)
            n = con.execute("UPDATE items SET status = CASE WHEN status = 'zip_member' THEN status ELSE 'meta_ok' END, "
                            "image_sha256 = NULL, stored_at = NULL WHERE image_sha256 = ?", (r["sha256"],)).rowcount
            con.execute("DELETE FROM image_hashes WHERE sha256 = ?", (r["sha256"],))
            con.execute("DELETE FROM image_matches WHERE sha_a = ? OR sha_b = ?", (r["sha256"], r["sha256"]))
            con.execute("DELETE FROM images WHERE sha256 = ?", (r["sha256"],))
            log.warning("repair: %s has no good copy; %d items queued for download again", r["sha256"][:12], n)
        else:
            for path in (primary, mirror):
                if path != good and (path == primary or r["mirrored_at"]):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(good, path)
            log.info("repair: %s restored from %s", r["sha256"][:12], good)
        con.commit()
    return problems


# ---------------------------------------------------------------- NAS (third copy, another room)

NAS_BWLIMIT_KBPS = 10_000      # ~10 MB/s


def nas_root(s: Settings) -> Path:
    """Mount the NAS share through gvfs (no root needed) and return our directory on it."""
    if not s.nas_share:
        raise StorageError("NAS not configured")
    server, share = s.nas_share.removeprefix("smb://").split("/", 1)
    mount = Path(f"/run/user/{os.getuid()}/gvfs/smb-share:server={server},share={share.lower()}")
    if not mount.exists():
        cred = dict(l.split("=", 1) for l in s.nas_credentials.read_text().splitlines() if "=" in l)
        answers = f"{cred['user']}\n{cred.get('domain', 'WORKGROUP')}\n{cred['password']}\n"
        subprocess.run(["gio", "mount", s.nas_share], input=answers, text=True, capture_output=True, timeout=60)
        if not mount.exists():
            raise StorageError(f"could not mount {s.nas_share}")
    root = mount / s.nas_dir
    root.mkdir(exist_ok=True)
    return root


def nas_sync(s: Settings, con: sqlite3.Connection) -> None:
    """Copy every primary disk's store (images + DB snapshots) to the NAS and record what arrived.

    The NAS keeps one namespace (images/ab/cd/<sha256>.ext), whichever HDD pair a file is on.
    A NAS problem is logged and never stops the crawl.
    """
    if not s.nas_share:
        return
    if "nas_at" not in [c[1] for c in con.execute("PRAGMA table_info(images)")]:
        con.execute("ALTER TABLE images ADD COLUMN nas_at TEXT")
    con.commit()        # never hold a write lock across the long rsync (other crawlers would time out)
    try:
        dst = nas_root(s)
    except Exception as e:
        log.warning("NAS unavailable, skipping third copy: %s", e)
        return
    for primary, _ in s.pairs:
        try:
            src = disk_root(s, primary)
        except StorageError:
            continue
        if not src.exists():
            continue
        log.info("NAS sync %s -> %s", src, dst)
        # gvfs/SMB has no Unix permissions or owners; compare by size+mtime
        # low priority and a bandwidth cap: the NAS copy must never get in the way of anything else
        opts = ["nice", "-n", "19", "ionice", "-c", "3", "rsync", "-rt", "--no-perms", "--no-owner", "--no-group",
                f"--bwlimit={NAS_BWLIMIT_KBPS}", "--exclude", "*.part"]
        r = subprocess.run(opts + ["--exclude", "/db/", f"{src}/", f"{dst}/"], capture_output=True, text=True)
        if r.returncode not in (0, 23, 24):
            log.warning("NAS rsync from %s failed (%d): %s", primary, r.returncode, r.stderr[-500:])
        if (src / "db").exists():
            # DB snapshots: mirror exactly (the HDD keeps the latest few; don't let old ones pile up on the NAS)
            subprocess.run(opts + ["--delete", f"{src}/db/", f"{dst}/db/"], capture_output=True, text=True)
    # check over the network first (slow), then record in one short transaction
    arrived, missing = [], 0
    for row in con.execute("SELECT sha256, bytes, rel_path FROM images WHERE nas_at IS NULL").fetchall():
        f = dst / row["rel_path"]
        if f.exists() and f.stat().st_size == row["bytes"]:
            arrived.append((row["sha256"],))
        else:
            missing += 1
    con.executemany("UPDATE images SET nas_at = datetime('now') WHERE sha256 = ?", arrived)
    con.commit()
    ok = len(arrived)
    log.info("NAS: %d new copies confirmed, %d not there yet", ok, missing)
