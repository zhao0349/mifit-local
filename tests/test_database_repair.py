"""Repair interrupted SQLite writes using only disposable synthetic accounts."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import database_manager as manager
import desktop_backend as backend
from test_database_manager import activity, database
from test_database_manager import service as service
from test_desktop_backend import isolated as isolated


def interrupt_writer(path, journal_mode):
    """Crash this disposable writer after cache spill, never a running app process."""
    script = r"""
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
if sys.argv[2] != 'default':
    connection.execute('PRAGMA journal_mode=' + sys.argv[2])
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=2')
if connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('UPDATE daily_activity SET steps=456')
    connection.commit()
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE daily_activity SET steps=999')
connection.executemany(
    'INSERT INTO daily_activity (id,provider,source_type,user_id,date,steps) '
    "VALUES (?,'mi_fitness','cloud_session','123456','2026-01-02',999)",
    [('unfinished-' + str(i),) for i in range(1500)],
)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path), journal_mode],
                   check=True, timeout=15, capture_output=True)


def rows(path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return connection.execute("SELECT date,steps FROM daily_activity ORDER BY date").fetchall()


def test_hot_rollback_journal_is_backed_up_and_recovered_without_uncommitted_rows(service):
    key, path = database()
    activity(path)
    interrupt_writer(path, "DELETE")
    journal = Path(str(path) + "-journal")
    original, log = path.read_bytes(), journal.read_bytes()
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        with pytest.raises(sqlite3.OperationalError) as failure:
            connection.execute("PRAGMA quick_check").fetchall()
    assert failure.value.sqlite_errorname == "SQLITE_READONLY_ROLLBACK"

    result = service.repair(key)

    assert result["status"] == "ok" and result["recovered"]
    backup = Path(result["backup_directory"]) / "original"
    assert (backup / path.name).read_bytes() == original
    assert (backup / journal.name).read_bytes() == log
    assert rows(path) == [("2026-01-01", 123)]
    assert service.inventory()["accounts"][0]["total_rows"] == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_corrupt_database_is_preserved_when_repair_cannot_validate_it(service):
    key, path = database()
    damaged = b"not a SQLite database\x00" * 300
    path.write_bytes(damaged)

    with pytest.raises(manager.DatabaseRepairError) as failure:
        service.repair(key)

    assert isinstance(failure.value, backend.DesktopError)
    details = failure.value.diagnostics
    assert {"stage", "exception_type", "message", "sqlite_errorcode", "sqlite_errorname",
            "database_path", "backup_directory", "backup_complete"}.issubset(details)
    assert details["backup_complete"]
    assert details["stage"] and details["message"]
    assert details["exception_type"] == "DatabaseError"
    assert details["sqlite_errorcode"] == sqlite3.SQLITE_NOTADB
    assert details["sqlite_errorname"] == "SQLITE_NOTADB"
    assert Path(details["database_path"]) == path
    assert path.read_bytes() == damaged
    backups = list((path.parent / "recovery").glob("*/original/mi_fitness.db"))
    assert len(backups) == 1 and backups[0].read_bytes() == damaged
    assert backups[0].parent.parent == Path(details["backup_directory"])


def test_busy_backend_rejects_repair_before_creating_a_backup(service):
    key, path = database()
    activity(path)
    original = path.read_bytes()
    with backend.interface_lock:
        with pytest.raises(backend.DesktopError, match="已有任务"):
            service.repair(key)
    assert path.read_bytes() == original
    assert not (path.parent / "recovery").exists()


def test_crashed_wal_writer_keeps_committed_records_readable(service):
    key, path = database()
    activity(path)
    backend._enable_cache_wal(path)
    interrupt_writer(path, "default")
    assert Path(str(path) + "-wal").is_file()

    assert rows(path) == [("2026-01-01", 456)]
    summary = service.summary(key)
    assert len(summary["days"]) == 1 and summary["days"][0]["steps"] == 456
    assert service.inventory()["accounts"][0]["total_rows"] == 1


def test_damaged_account_deletion_waits_five_seconds_and_preserves_backups_and_other_accounts(service):
    _, other = database("678901")
    activity(other, user="678901")
    other_before = other.read_bytes()
    key, path = database()
    damaged = b"synthetic damaged database\x00" * 300
    path.write_bytes(damaged)
    with pytest.raises(manager.DatabaseRepairError) as failure:
        service.repair(key)
    backup = Path(failure.value.diagnostics["backup_directory"]) / "original" / path.name
    sidecars = [Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"synthetic interrupted log")
    preserved = path.parent / "keep.txt"
    preserved.write_bytes(b"unrelated account file")

    preview = service.preview_damaged(key)
    assert preview["all_data"] and preview["total_rows"] is None
    assert preview["wait_seconds"] == 5
    with backend.interface_lock:
        with pytest.raises(backend.DesktopError, match="已有任务"):
            service.preview_damaged(key)
        with pytest.raises(backend.DesktopError, match="已有任务"):
            service.delete(preview["token"])
    with pytest.raises(backend.DesktopError):
        service.delete(preview["token"])
    assert path.read_bytes() == damaged and all(sidecar.exists() for sidecar in sidecars)
    service.clock[0] += 5

    result = service.delete(preview["token"])

    assert result["status"] == "ok" and result["database_removed"]
    assert not path.exists() and not any(sidecar.exists() for sidecar in sidecars)
    assert backup.read_bytes() == damaged
    assert other.read_bytes() == other_before
    assert preserved.read_bytes() == b"unrelated account file"


def test_damaged_deletion_entry_rejects_a_healthy_database(service):
    key, path = database()
    activity(path)
    original = path.read_bytes()
    with pytest.raises(backend.DesktopError):
        service.preview_damaged(key)
    assert path.read_bytes() == original
    assert rows(path) == [("2026-01-01", 123)]
