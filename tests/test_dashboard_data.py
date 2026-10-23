"""Only synthetic SQLite fixtures are opened by these tests."""

import json
import sqlite3
from contextlib import closing

import pytest

import dashboard_data as dashboard


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(dashboard._DEMO_SCHEMA)
    return path


def insert(path, table, rows):
    if not rows:
        return
    with sqlite3.connect(path) as connection:
        placeholders = ",".join("?" for _ in rows[0])
        connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)


def one_day(summary, day="2026-07-15"):
    return next(item for item in summary["days"] if item["date"] == day)


def test_activity_chooses_one_device_record_and_preserves_zero_and_missing(database):
    insert(database, "daily_activity", [
        ("2026-07-15", 5000, 3400, 200, None, "2026-07-15T20:00:00+08:00"),
        ("2026-07-15", 6000, 4200, 250, 45, "2026-07-15T21:00:00+08:00"),
        ("2026-07-15", 6000, 4300, 260, None, "2026-07-15T22:00:00+08:00"),
        ("2026-07-16", 0, 0, 0, 0, None),
    ])
    summary = dashboard.build_summary(database)
    day = one_day(summary)
    assert (day["steps"], day["distance_km"], day["active_kcal"]) == (6000, 4.3, 260)
    assert day["active_minutes"] is None
    assert day["sleep_minutes"] is None and day["weight_kg"] is None
    assert day["workout_count"] == 0 and day["workout_minutes"] is None
    assert all(value is None for value in day["sleep_stages"].values())
    assert one_day(summary, "2026-07-16")["steps"] == 0
    assert summary["meta"]["source_counts"]["daily_activity"] == 4


def test_timestamp_boundaries_valid_samples_and_resting_only(database):
    insert(database, "heart_rate_samples", [
        ("2026-07-14T15:59:59Z", 99, "resting"),
        ("2026-07-14T16:00:00Z", 60, "resting"),
        ("2026-07-15T23:59:59+08:00", 90, "active"),
        ("2026-07-16T00:00:00+08:00", 105, "active"),
        ("2026-07-15T08:00:00", 0, "resting"),
        ("2026-07-15T09:00:00+08:00", -1, "resting"),
    ])
    insert(database, "spo2_samples", [
        ("2026-07-15T00:00:00+08:00", 0),
        ("2026-07-15T01:00:00+08:00", 98),
        ("2026-07-15T02:00:00+08:00", 100),
    ])
    insert(database, "stress_samples", [
        ("2026-07-15T00:00:00+08:00", 0),
        ("2026-07-15T01:00:00+08:00", 20),
    ])
    summary = dashboard.build_summary(database)
    day = one_day(summary)
    assert (day["heart_rate_avg"], day["heart_rate_min"], day["heart_rate_max"]) == (75, 60, 90)
    assert day["resting_hr"] == 60
    assert day["spo2_avg"] == 99 and day["stress_avg"] == 10
    assert one_day(summary, "2026-07-14")["heart_rate_avg"] == 99
    assert one_day(summary, "2026-07-16")["heart_rate_avg"] == 105


def test_sleep_uses_wake_day_longest_session_and_nonoverlapping_naps(database):
    stages = json.dumps([
        {"stage": "deep", "minutes": 80}, {"stage": "light", "minutes": 300},
        {"stage": "rem", "minutes": 90}, {"stage": "awake", "minutes": 10},
    ])
    insert(database, "sleep_sessions", [
        ("2026-07-14T23:00:00+08:00", "2026-07-15T07:00:00+08:00", 480, 470, 85, 0, stages),
        ("2026-07-14T23:15:00+08:00", "2026-07-15T06:50:00+08:00", 455, 440, 80, 0, "[]"),
        ("2026-07-15T13:00:00+08:00", "2026-07-15T13:35:00+08:00", 35, 30, None, 1, "[]"),
        ("2026-07-15T13:05:00+08:00", "2026-07-15T13:30:00+08:00", 25, 20, None, 1, "[]"),
        ("2026-07-15T16:00:00+08:00", "2026-07-15T16:25:00+08:00", 25, 20, None, 1, "[]"),
    ])
    summary = dashboard.build_summary(database)
    day = one_day(summary)
    assert summary["meta"]["range"] == {"start": "2026-07-15", "end": "2026-07-15"}
    assert day["sleep_minutes"] == 470 and day["sleep_score"] == 85
    assert day["sleep_stages"] == {"deep": 80, "light": 300, "rem": 90, "awake": 10}
    assert day["nap_minutes"] == 50


def test_latest_body_record_does_not_fill_missing_fields_from_earlier_measurement(database):
    insert(database, "body_measurements", [
        ("2026-07-15T07:00:00+08:00", 70, 20),
        ("2026-07-15T01:00:00Z", 71, None),
        ("2026-07-15T17:00:00Z", 72, 21),
    ])
    summary = dashboard.build_summary(database)
    assert one_day(summary)["weight_kg"] == 71
    assert one_day(summary)["body_fat_pct"] is None
    assert one_day(summary, "2026-07-16")["weight_kg"] == 72


def test_workouts_have_only_approved_fields_and_do_not_invent_pace(database):
    insert(database, "workouts", [
        ("2026-07-14T16:30:00Z", "running", 30, 5000, 250, 145, None),
        ("2026-07-15T18:00:00+08:00", "strength", None, None, None, 0, 0),
    ])
    summary = dashboard.build_summary(database)
    day = one_day(summary)
    assert day["workout_count"] == 2 and day["workout_minutes"] == 30
    assert summary["workouts"][0] == {
        "date": "2026-07-15", "start": "00:30", "type": "running",
        "duration_minutes": 30, "distance_km": 5, "calories_kcal": 250,
        "avg_heart_rate_bpm": 145, "avg_pace_sec_per_km": None,
    }
    assert summary["workouts"][1]["avg_heart_rate_bpm"] is None


def test_no_identifiers_raw_samples_or_gps_leave_aggregator(database):
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE daily_activity ADD COLUMN user_id TEXT")
        connection.execute("ALTER TABLE daily_activity ADD COLUMN device_id TEXT")
        connection.execute(
            "INSERT INTO daily_activity VALUES (?,?,?,?,?,?,?,?)",
            ("2026-07-15", 10, 5, 1, 1, None, "private-account-marker", "private-device-marker"),
        )
        connection.execute("CREATE TABLE workout_gps_points (latitude REAL, longitude REAL)")
        connection.execute("INSERT INTO workout_gps_points VALUES (12.12345678,34.12345678)")
        connection.execute("CREATE TABLE workout_detail_samples (private_marker TEXT)")
        connection.execute("INSERT INTO workout_detail_samples VALUES ('private-sample-marker')")
    summary = dashboard.build_summary(database)
    text = json.dumps(summary, ensure_ascii=False)
    for forbidden in ("private-account-marker", "private-device-marker", "private-sample-marker",
                      "12.12345678", "34.12345678", '"user_id"', '"device_id"', '"latitude"'):
        assert forbidden not in text
    assert summary["meta"]["source_counts"]["workout_gps"] == 1
    assert summary["meta"]["source_counts"]["workout_detail"] == 1


def test_readonly_transaction_and_original_bytes_unchanged(database, monkeypatch):
    insert(database, "heart_rate_samples", [("2026-07-15T00:00:00+08:00", 70, "resting")])
    before = database.read_bytes()
    real_connect = sqlite3.connect
    statements = []

    def checked_connect(database_uri, *args, **kwargs):
        assert "mode=ro" in database_uri and kwargs.get("uri")
        connection = real_connect(database_uri, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(dashboard.sqlite3, "connect", checked_connect)
    dashboard.build_summary(database)
    assert database.read_bytes() == before
    assert "BEGIN" in statements and "ROLLBACK" in statements
    assert "PRAGMA query_only = ON" in statements
    assert not any(sql.startswith(("INSERT", "DELETE", "UPDATE", "CREATE")) for sql in statements)


def test_empty_missing_tables_and_invalid_values_are_not_zero_filled(database):
    empty = dashboard.build_summary(database)
    assert empty["days"] == [] and empty["workouts"] == []
    assert empty["meta"]["range"] == {"start": None, "end": None}
    insert(database, "heart_rate_samples", [
        ("2026-07-15T00:00:00+08:00", 0, "resting"),
        ("sensitive-invalid-date", 80, "resting"),
    ])
    summary = dashboard.build_summary(database)
    assert one_day(summary)["heart_rate_avg"] is None
    assert "sensitive-invalid-date" not in json.dumps(summary)
    assert any("日期或时间无效" in note for note in summary["meta"]["notes"])


def test_nonfinite_and_out_of_range_measurements_are_missing(database):
    insert(database, "heart_rate_samples", [
        ("2026-07-15T00:00:00+08:00", "Infinity", "resting"),
        ("2026-07-15T01:00:00+08:00", None, "resting"),
        ("2026-07-15T02:00:00+08:00", "not-a-number", "passive"),
    ])
    insert(database, "spo2_samples", [("2026-07-15T00:00:00+08:00", 101)])
    insert(database, "stress_samples", [("2026-07-15T00:00:00+08:00", -1)])
    summary = dashboard.build_summary(database)
    day = one_day(summary)
    assert all(day[key] is None for key in (
        "heart_rate_avg", "heart_rate_min", "heart_rate_max", "resting_hr", "spo2_avg", "stress_avg",
    ))
    json.dumps(summary, allow_nan=False)


def test_missing_sleep_stages_remain_missing_and_zero_awake_is_preserved(database):
    insert(database, "sleep_sessions", [
        ("2026-07-14T23:00:00+08:00", "2026-07-15T07:00:00+08:00", 480, None, None, 0,
         json.dumps([{"stage": "awake", "minutes": 0}, {"stage": "deep", "minutes": -10}])),
    ])
    day = one_day(dashboard.build_summary(database))
    assert day["sleep_minutes"] is None and day["sleep_score"] is None
    assert day["sleep_stages"] == {"deep": None, "light": None, "rem": None, "awake": 0}


def test_single_snapshot_is_consistent_across_concurrent_writer(database, monkeypatch):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO daily_activity VALUES ('2026-07-15',100,70,5,1,NULL)"
        )
        connection.execute(
            "INSERT INTO heart_rate_samples VALUES ('2026-07-15T12:00:00+08:00',70,'resting')"
        )
        connection.commit()
    original_rows = dashboard._rows
    writer_ran = False

    def concurrent_rows(connection, schema, table, columns):
        nonlocal writer_ran
        if table == "heart_rate_samples" and not writer_ran:
            with sqlite3.connect(database) as writer:
                writer.execute("UPDATE heart_rate_samples SET bpm=100")
            writer_ran = True
        return original_rows(connection, schema, table, columns)

    monkeypatch.setattr(dashboard, "_rows", concurrent_rows)
    summary = dashboard.build_summary(database)
    assert writer_ran
    assert one_day(summary)["heart_rate_avg"] == 70
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT bpm FROM heart_rate_samples").fetchone()[0] == 100


def test_resolver_is_local_and_blocks_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    settings = tmp_path / "data" / "settings.json"
    settings.parent.mkdir()
    key = "a" * 64
    target = settings.parent / key / "mi_fitness.db"
    target.parent.mkdir()
    target.touch()
    settings.write_text(json.dumps({"account_key": key}), encoding="utf-8")
    assert dashboard.resolve_current_database() == target.resolve()
    settings.write_text(json.dumps({"account_key": "../../outside"}), encoding="utf-8")
    with pytest.raises(dashboard.DashboardDataError) as error:
        dashboard.resolve_current_database()
    assert str(tmp_path) not in str(error.value)


def test_demo_is_deterministic_full_year_synthetic_with_gaps_and_missing_values(monkeypatch):
    monkeypatch.setattr(dashboard, "resolve_current_database", lambda: pytest.fail("read settings"))
    first = dashboard.demo_summary()
    second = dashboard.demo_summary()
    assert first == second
    assert first["meta"]["range"] == {"start": "2025-10-21", "end": "2026-10-20"}
    assert 300 < len(first["days"]) < 365
    assert 100 < len(first["workouts"]) < 180
    assert any(day["steps"] is None for day in first["days"])
    assert any(day["steps"] == 0 for day in first["days"])
    assert len({workout["type"] for workout in first["workouts"]}) >= 5
    assert "合成" in first["meta"]["notes"][0]
    json.dumps(first, allow_nan=False)


def test_large_sample_table_is_iterated_without_fetchall(database, monkeypatch):
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO heart_rate_samples VALUES (?,?,?)",
            (("2026-07-15T12:00:00+08:00", 60 + index % 3, "passive") for index in range(10000)),
        )
    original_rows = dashboard._rows

    class IterationOnly:
        def __init__(self, rows):
            self.rows = rows

        def __iter__(self):
            return iter(self.rows)

        def fetchall(self):
            pytest.fail("raw records must not be loaded with fetchall")

    monkeypatch.setattr(
        dashboard, "_rows",
        lambda *args: IterationOnly(original_rows(*args)),
    )
    summary = dashboard.build_summary(database)
    assert summary["meta"]["source_counts"]["heart_rate"] == 10000
    assert one_day(summary)["heart_rate_avg"] == 61
    assert len(summary["days"]) == 1


def test_source_errors_do_not_echo_path_or_sqlite_details(tmp_path):
    invalid = tmp_path / "private-source-name.db"
    invalid.write_bytes(b"not a database")
    with pytest.raises(dashboard.DashboardDataError) as error:
        dashboard.build_summary(invalid)
    assert "private-source-name" not in str(error.value)
    assert str(tmp_path) not in str(error.value)
