"""Database backups: consistency, compression, rotation, scheduling, restore."""
import gzip
import sqlite3
from datetime import datetime, timedelta

import pytest

import backup


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "time_capsule.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, email TEXT)")
    con.executemany("INSERT INTO user (email) VALUES (?)", [(f"u{i}@example.com",) for i in range(50)])
    con.commit()
    con.close()
    return path


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM user").fetchone()[0]
    finally:
        con.close()


def test_backup_is_a_valid_compressed_copy(db_file, tmp_path):
    out = backup.backup_database(str(db_file), tmp_path / "b")
    assert out.name.startswith("time_capsule-") and out.name.endswith(".db.gz")
    restored = tmp_path / "restored.db"
    with gzip.open(out) as f_in, open(restored, "wb") as f_out:
        f_out.write(f_in.read())
    assert _rows(restored) == 50


def test_backup_while_a_write_transaction_is_open(db_file, tmp_path):
    # Simulates the app mid-request: uncommitted rows must not leak in,
    # and the copy must still be consistent.
    writer = sqlite3.connect(db_file)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO user (email) VALUES ('pending@example.com')")
    try:
        out = backup.backup_database(str(db_file), tmp_path / "b")
    finally:
        writer.rollback()
        writer.close()
    restored = tmp_path / "r.db"
    backup.restore_database(str(out), str(restored))
    assert _rows(restored) == 50


def test_keeps_only_newest_n(db_file, tmp_path):
    dest = tmp_path / "b"
    t0 = datetime(2026, 1, 1)
    for i in range(5):
        backup.backup_database(str(db_file), dest, keep=3, now=t0 + timedelta(days=i))
    names = [p.name for p in backup.list_backups(dest)]
    assert names == ["time_capsule-20260103-000000.db.gz",
                     "time_capsule-20260104-000000.db.gz",
                     "time_capsule-20260105-000000.db.gz"]


def test_backup_if_due_runs_once_a_day(db_file, tmp_path):
    dest = tmp_path / "b"
    t0 = datetime(2026, 1, 1, 9)
    assert backup.backup_if_due(str(db_file), dest, now=t0) is not None       # first ever
    assert backup.backup_if_due(str(db_file), dest, now=t0 + timedelta(hours=5)) is None
    assert backup.backup_if_due(str(db_file), dest, now=t0 + timedelta(hours=25)) is not None
    assert len(backup.list_backups(dest)) == 2


def test_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.backup_database(str(tmp_path / "nope.db"), tmp_path / "b")


def test_restore_replaces_database(db_file, tmp_path):
    out = backup.backup_database(str(db_file), tmp_path / "b")
    con = sqlite3.connect(db_file)
    con.execute("DELETE FROM user")
    con.commit()
    con.close()
    assert _rows(db_file) == 0
    backup.restore_database(str(out), str(db_file))
    assert _rows(db_file) == 50


def test_default_location_is_next_to_database(db_file):
    assert backup.backup_dir_for(str(db_file)) == db_file.parent / "backups"


def test_app_makes_daily_backup_on_request(file_client, file_app_ctx, monkeypatch):
    import app as A
    monkeypatch.setenv("RUN_JOBS_ON_REQUEST", "1")
    monkeypatch.setattr(A, "_last_job_run", None)
    db_path = A.db.engine.url.database
    dest = backup.backup_dir_for(db_path)
    assert backup.list_backups(dest) == []

    file_client.post("/api/login", json={})          # any request triggers the jobs
    assert len(backup.list_backups(dest)) == 1

    monkeypatch.setattr(A, "_last_job_run", None)     # same day again: no second copy
    file_client.post("/api/login", json={})
    assert len(backup.list_backups(dest)) == 1
