"""Synthetic SQLite checks for account-scoped, confirmed cache deletion."""

import os
import sqlite3
from contextlib import closing

import pytest
from mi_fitness_mcp.storage import Database

import database_manager as manager
import desktop_backend as backend
from test_desktop_backend import isolated as isolated


@pytest.fixture
def service(isolated, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(manager.time, "monotonic", lambda: clock[0])
    instance = manager.DatabaseManager()
    instance.clock = clock
    return instance


def database(user="123456", region="cn"):
    backend.save_credentials(user, "synthetic-token", region)
    key = backend.account_scope()
    path = backend.DATA_DIR / key / "mi_fitness.db"
    Database(path)
    return key, path


def insert(path, table, identifier, *, user="123456", **fields):
    values = {"id": identifier, "provider": "mi_fitness", "source_type": "cloud_session",
              "user_id": user, **fields}
    with closing(sqlite3.connect(path)) as connection:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values()))
        connection.commit()


def activity(path, day="2026-01-01", *, user="123456"):
    insert(path, "daily_activity", day, user=user, date=day, steps=123)


def count(path, table):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_inventory_reports_accounts_all_seven_categories_ranges_and_actual_bytes(service):
    first, path = database()
    activity(path)
    insert(path, "abnormal_heart_beat_events", "abnormal", event_id="event-1",
           start_at="2026-01-02T16:00:00Z", end_at="2026-01-02T16:01:00Z", duration_seconds=60)
    insert(path, "workout_detail_samples", "orphan", workout_id="orphan-workout",
           offset_seconds=0, timestamp="2026-01-04T00:00:00+08:00")
    second, other = database("678901", "us")
    activity(other, "2025-02-01", user="678901")
    listing = service.inventory()
    accounts = {item["account_key"]: item for item in listing["accounts"]}
    assert len(accounts) == 2 and len(listing["categories"]) == 7
    assert accounts[first]["total_rows"] == 3
    assert (accounts[first]["start"], accounts[first]["end"]) == ("2026-01-01", "2026-01-04")
    assert accounts[first]["database_bytes"] == path.stat().st_size
    assert not accounts[first]["current"] and accounts[second]["current"]
    assert accounts[second]["region"] == "us"
    assert listing["total_bytes"] == path.stat().st_size + other.stat().st_size
    assert "synthetic-token" not in str(listing)


def test_summary_reads_selected_cache_without_changing_credentials_or_database(service):
    first, path = database()
    activity(path, "2026-01-01")
    second, other = database("678901")
    activity(other, "2026-01-02", user="678901")
    before = path.read_bytes()
    summary = service.summary(first)
    assert [row["date"] for row in summary["days"]] == ["2026-01-01"]
    assert backend.account_scope() == second
    assert path.read_bytes() == before
    assert service.summary(first, start="2026-01-02", end="2026-01-02")["days"] == []


def test_date_and_category_are_intersected_and_body_measurement_is_whole_row(service):
    key, path = database()
    activity(path)
    for day in (1, 2):
        insert(path, "body_measurements", f"body-{day}", timestamp=f"2026-01-0{day}T12:00:00",
               weight_kg=60, body_fat_pct=20, bmi=22)
        insert(path, "heart_rate_samples", f"heart-{day}", timestamp=f"2026-01-0{day}T12:00:00",
               bpm=70, sample_type="passive")
    preview = service.preview(key, "2026-01-01", "2026-01-01", ["body_measurements"])
    assert preview["total_rows"] == 1 and not preview["all_data"]
    result = service.delete(preview["token"])
    assert result["deleted_rows"] == 1 and result["compacted"]
    assert count(path, "body_measurements") == 1
    assert count(path, "heart_rate_samples") == 2
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT weight_kg,body_fat_pct,bmi FROM body_measurements").fetchone() == (60, 20, 22)


def test_heart_rate_category_includes_abnormal_events_and_shanghai_boundaries(service):
    key, path = database()
    activity(path)
    for index, stamp in enumerate(("2025-12-31T15:59:59Z", "2025-12-31T16:00:00Z",
                                   "2026-01-01T15:59:59Z", "2026-01-01T16:00:00Z")):
        insert(path, "heart_rate_samples", f"heart-{index}", timestamp=stamp, bpm=70, sample_type="passive")
    insert(path, "abnormal_heart_beat_events", "abnormal", event_id="event-1",
           start_at="2026-01-01T12:00:00", end_at="2026-01-01T12:01:00", duration_seconds=60)
    preview = service.preview(key, "2026-01-01", "2026-01-01", ["heart_rate"])
    assert preview["total_rows"] == 3
    service.delete(preview["token"])
    assert count(path, "heart_rate_samples") == 2
    assert count(path, "abnormal_heart_beat_events") == 0
    assert count(path, "daily_activity") == 1


def test_sleep_date_is_waking_date(service):
    key, path = database()
    activity(path)
    insert(path, "sleep_sessions", "sleep", sleep_id="sleep-1", start_at="2026-01-01T23:00:00",
           end_at="2026-01-02T07:00:00", duration_minutes=480, time_asleep_minutes=450,
           time_awake_minutes=30)
    assert service.preview(key, "2026-01-01", "2026-01-01", ["sleep"])["total_rows"] == 0
    preview = service.preview(key, "2026-01-02", "2026-01-02", ["sleep"])
    assert preview["total_rows"] == 1
    service.delete(preview["token"])
    assert count(path, "sleep_sessions") == 0


def test_partial_delete_invalidates_selected_sync_types_and_keeps_other_coverage(service):
    key, path = database()
    activity(path)
    insert(path, "sleep_sessions", "sleep", sleep_id="sleep-1", start_at="2026-01-01T23:00:00",
           end_at="2026-01-02T07:00:00", duration_minutes=480, time_asleep_minutes=450,
           time_awake_minutes=30)
    insert(path, "heart_rate_samples", "heart", timestamp="2026-01-02T12:00:00",
           bpm=70, sample_type="passive")
    insert(path, "abnormal_heart_beat_events", "abnormal", event_id="event-1",
           start_at="2026-01-02T12:00:00", end_at="2026-01-02T12:01:00", duration_seconds=60)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"CREATE TABLE {manager.SYNC_COVERAGE_TABLE} "
            "(data_type TEXT, date TEXT, PRIMARY KEY(data_type,date))"
        )
        connection.executemany(f"INSERT INTO {manager.SYNC_COVERAGE_TABLE} VALUES (?,?)", [
            ("sleep", "2026-01-01"), ("sleep", "2026-01-03"),
            ("heart_rate", "2026-01-02"), ("abnormal_heart_beat", "2026-01-02"),
            ("daily_activity", "2026-01-01"), ("stress", "2026-01-02"),
        ])
        connection.commit()
    account = service.inventory()["accounts"][0]
    assert account["account_key"] == key and account["total_rows"] == 4
    assert len(account["categories"]) == 7
    preview = service.preview(key, "2026-01-02", "2026-01-02", ["sleep", "heart_rate"])
    assert preview["total_rows"] == 3 and not preview["all_data"]
    service.delete(preview["token"])
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            f"SELECT data_type,date FROM {manager.SYNC_COVERAGE_TABLE} ORDER BY data_type,date"
        ).fetchall() == [("daily_activity", "2026-01-01"), ("stress", "2026-01-02")]
    assert service.inventory()["accounts"][0]["total_rows"] == 1


def test_workout_category_removes_whole_cross_midnight_record_and_orphan_group(service):
    key, path = database()
    activity(path)
    insert(path, "workouts", "workout", workout_id="w1", activity_type="run",
           start_at="2026-01-01T23:59:00", end_at="2026-01-02T00:01:00", duration_minutes=2)
    for group in ("w1", "orphan"):
        for index, stamp in enumerate(("2026-01-01T23:59:00", "2026-01-02T00:01:00")):
            insert(path, "workout_detail_samples", f"detail-{group}-{index}", workout_id=group,
                   offset_seconds=index * 120, timestamp=stamp, heart_rate_bpm=90)
            insert(path, "workout_gps_points", f"gps-{group}-{index}", workout_id=group,
                   offset_seconds=index * 120, timestamp=stamp, latitude=1.0, longitude=1.0)
    preview = service.preview(key, "2026-01-01", "2026-01-01", ["workouts"])
    assert preview["total_rows"] == 9
    service.delete(preview["token"])
    assert all(count(path, table) == 0 for table in ("workouts", *manager.CHILD_TABLES))
    assert count(path, "daily_activity") == 1


@pytest.mark.parametrize("filters", [
    {}, {"categories": [key for key, _ in manager.CATEGORIES]},
    {"start": "2025-01-01", "end": "2027-01-01", "categories": ["daily_activity"]},
])
def test_any_actual_full_account_delete_waits_three_seconds_and_preserves_other_account(service, filters):
    key, path = database()
    activity(path)
    _, other = database("678901")
    activity(other, user="678901")
    original = other.read_bytes()
    preview = service.preview(key, **filters)
    assert preview["all_data"] and preview["wait_seconds"] == 3
    with pytest.raises(backend.DesktopError, match="3 秒"):
        service.delete(preview["token"])
    assert path.exists()
    service.clock[0] += 3
    result = service.delete(preview["token"])
    assert result["database_removed"] and result["database_bytes"] == 0
    assert not path.exists() and other.read_bytes() == original
    assert len(service.inventory()["accounts"]) == 1
    with pytest.raises(backend.DesktopError, match="过期"):
        service.delete(preview["token"])


def test_preview_is_read_only_and_expired_confirmation_cannot_delete(service):
    key, path = database()
    activity(path)
    original = path.read_bytes()
    preview = service.preview(key)
    assert path.read_bytes() == original
    service.clock[0] += manager.TTL_SECONDS
    with pytest.raises(backend.DesktopError, match="过期"):
        service.delete(preview["token"])
    assert path.read_bytes() == original


def test_changed_database_rejects_confirmation_even_with_restored_mtime(service):
    key, path = database()
    activity(path)
    preview = service.preview(key)
    before = path.stat()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE daily_activity SET steps=999")
        connection.commit()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    service.clock[0] += 3
    with pytest.raises(backend.DesktopError, match="数据库已变化"):
        service.delete(preview["token"])
    assert count(path, "daily_activity") == 1


def test_wal_inventory_and_preview_include_uncheckpointed_rows_and_detect_wal_change(service):
    key, path = database()
    activity(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE daily_activity SET steps=456")
        connection.commit()
        main_before = path.read_bytes()
        listing = service.inventory()["accounts"][0]
        assert listing["total_rows"] == 1 and listing["wal_bytes"] > 0
        assert listing["database_bytes"] == listing["db_bytes"] + listing["wal_bytes"] + listing["shm_bytes"]
        preview = service.preview(key)
        connection.execute("UPDATE daily_activity SET steps=789")
        connection.commit()
        assert path.read_bytes() == main_before
        service.clock[0] += 3
        with pytest.raises(backend.DesktopError, match="数据库已变化"):
            service.delete(preview["token"])
        assert count(path, "daily_activity") == 1
    finally:
        connection.close()


def test_wal_partial_delete_keeps_unselected_records(service):
    key, path = database()
    activity(path)
    activity(path, "2026-01-02")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE daily_activity SET steps=456")
        connection.commit()
        preview = service.preview(key, "2026-01-01", "2026-01-01", ["daily_activity"])
        result = service.delete(preview["token"])
        assert result["deleted_rows"] == 1
        assert count(path, "daily_activity") == 1
    finally:
        connection.close()


def test_full_delete_handles_closed_database_with_persistent_wal_mode(service):
    key, path = database()
    activity(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    preview = service.preview(key)
    service.clock[0] += 3
    assert service.delete(preview["token"])["database_removed"]
    assert not path.exists()


@pytest.mark.parametrize("key", [None, "", "all", "../other", ["a", "b"]])
def test_a_single_valid_account_is_required(service, key):
    with pytest.raises(ValueError):
        service.preview(key)


def test_no_field_level_deletion_and_existing_sync_blocks_database_management(service):
    key, path = database()
    activity(path)
    with pytest.raises(ValueError):
        service.preview(key, categories=["weight_kg"])
    with backend.interface_lock:
        with pytest.raises(backend.DesktopError, match="已有任务"):
            service.preview(key)
    assert count(path, "daily_activity") == 1


def test_transaction_rolls_back_all_categories_when_one_delete_fails(service, monkeypatch):
    key, path = database()
    activity(path)
    insert(path, "heart_rate_samples", "heart", timestamp="2026-01-01T12:00:00", bpm=70, sample_type="passive")
    insert(path, "abnormal_heart_beat_events", "abnormal", event_id="event-1",
           start_at="2026-01-01T12:00:00", end_at="2026-01-01T12:01:00", duration_seconds=60)
    preview = service.preview(key, categories=["heart_rate"])
    original_connect = manager._connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if kwargs.get("write"):
            connection.set_authorizer(lambda action, first, *_: sqlite3.SQLITE_DENY
                                      if action == sqlite3.SQLITE_DELETE and first == "abnormal_heart_beat_events"
                                      else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(manager, "_connect", connect)
    with pytest.raises(backend.DesktopError):
        service.delete(preview["token"])
    assert count(path, "heart_rate_samples") == 1
    assert count(path, "abnormal_heart_beat_events") == 1
