"""
SQLite backups.

Uses SQLite's online backup API, so a consistent copy is taken even while the
app is serving requests (a plain file copy can catch a half-written page).
Each copy is integrity-checked, gzip-compressed, and the newest KEEP files
are kept.

Backups live next to the database in instance/backups/ (git-ignored). They
protect against bad migrations and accidental deletes, not against losing the
server: download one now and then for an off-server copy. They are never sent
to R2, whose bucket is public.
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

KEEP = 14
BACKUP_INTERVAL = timedelta(hours=24)
PREFIX = "time_capsule-"
SUFFIX = ".db.gz"


def backup_dir_for(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "backups"


def list_backups(dest_dir: Path) -> list[Path]:
    """Backups in dest_dir, oldest first (names sort chronologically)."""
    if not dest_dir.is_dir():
        return []
    return sorted(p for p in dest_dir.iterdir()
                  if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX))


def latest_backup_time(dest_dir: Path) -> datetime | None:
    backups = list_backups(dest_dir)
    if not backups:
        return None
    stamp = backups[-1].name[len(PREFIX):-len(SUFFIX)]
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def backup_database(db_path: str, dest_dir: Path | None = None, keep: int = KEEP,
                    now: datetime | None = None) -> Path:
    """Write a verified, compressed backup of db_path and prune old ones.

    Returns the path of the new backup. Raises if the source is missing or
    the copy fails its integrity check (in which case nothing is kept).
    """
    src = Path(db_path)
    if not src.is_file():
        raise FileNotFoundError(f"Database not found: {src}")
    dest_dir = dest_dir or backup_dir_for(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.utcnow()
    target = dest_dir / f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}"

    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        raw = Path(tmp) / "snapshot.db"
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        copy = sqlite3.connect(raw)
        try:
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
            source.close()
        if result != "ok":
            raise RuntimeError(f"Backup failed integrity check: {result}")
        with open(raw, "rb") as f_in, gzip.open(target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    for old in list_backups(dest_dir)[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def restore_database(backup_path: str, db_path: str) -> None:
    """Replace db_path with a backup. Stop the app (Web tab) first."""
    with gzip.open(backup_path, "rb") as f_in, open(db_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def backup_if_due(db_path: str, dest_dir: Path | None = None,
                  now: datetime | None = None) -> Path | None:
    """Back up if the newest backup is older than BACKUP_INTERVAL."""
    dest_dir = dest_dir or backup_dir_for(db_path)
    now = now or datetime.utcnow()
    last = latest_backup_time(dest_dir)
    if last and now - last < BACKUP_INTERVAL:
        return None
    if os.path.getsize(db_path) == 0:
        return None
    return backup_database(db_path, dest_dir, now=now)
