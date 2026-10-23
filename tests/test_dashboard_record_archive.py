"""Synthetic end-to-end contracts for the full export detail archive."""

import json
import sqlite3

import pytest

import dashboard_data
from test_dashboard_worker import document, run_cases, run_document


def unpack(summary, source):
    dataset = summary["record_details"]["datasets"][source]
    return [dict(zip(dataset["fields"], row, strict=True)) for row in dataset["rows"]]


def test_body_composition_all_measurements_and_summary_round_trip():
    first = {
        "timestamp": "2026-10-21T08:00:00+08:00", "weight_kg": 70,
        "bmi": 22.8, "body_fat_pct": 20, "muscle_mass_kg": 51.12345,
        "water_pct": 56.5, "bone_mass_kg": 2.7, "visceral_fat_score": 0,
        "basal_metabolism_kcal": 1600, "metabolic_age": 30,
    }
    second = {"timestamp": "2026-10-21T20:00:00+08:00", "weight_kg": 71, "bmi": 23}
    summary = run_document(document({"body_measurements": [first, first, second]}))
    rows = unpack(summary, "body_measurements")
    assert len(rows) == 3 and rows[0] == rows[1] == first
    assert rows[2]["muscle_mass_kg"] is None
    day = summary["days"][0]
    assert day["weight_kg"] == 71 and day["bmi"] == 23
    assert day["water_pct"] is None and day["muscle_mass_kg"] is None
    assert run_document(summary)["record_details"] == summary["record_details"]


def test_all_datasets_known_columns_signed_values_and_identifiers():
    schema = json.loads(dashboard_data.ROOT.joinpath("dashboard/record-schema.json").read_text(encoding="utf-8"))
    records = {}
    for source, definition in schema.items():
        record = {}
        for key, _, _, kind in definition["fields"]:
            record[key] = (
                [{"stage": "deep", "minutes": 0}] if kind == "stages" else
                False if kind == "boolean" else
                "2026-10-21T08:00:00+08:00" if kind == "text" else 0
            )
        record[definition["date"]] = "2026-10-21" if source == "daily_activity" else "2026-10-21T08:00:00+08:00"
        record.update(user_id="PRIVATE_SENTINEL", device_id="PRIVATE_SENTINEL",
                      fds_sid="PRIVATE_SENTINEL", unknown_secret="PRIVATE_SENTINEL")
        records[source] = [record]
    records["workout_gps"][0].update(latitude=-12.123456789, longitude=-34.987654321, altitude_m=-8)
    records["workout_detail"][0]["offset_seconds"] = -2
    summary = run_document(document(records))
    for source, definition in schema.items():
        row = unpack(summary, source)[0]
        assert set(row) == {field[0] for field in definition["fields"]}
        assert row == {key: records[source][0][key] for key in row}
    assert "PRIVATE_SENTINEL" not in json.dumps(summary)
    assert run_document(summary)["record_details"] == summary["record_details"]


def test_detail_only_dates_and_invalid_dates_are_not_lost():
    summary = run_document(document({
        "workout_gps": [
            {"timestamp": "2026-10-20T16:00:00Z", "latitude": -1, "longitude": 2},
            {"timestamp": "invalid", "latitude": 0, "longitude": 0},
        ],
        "abnormal_heart_beat": [{"start_at": "2026-10-22T09:00:00", "duration_seconds": 0}],
    }))
    assert [day["date"] for day in summary["days"]] == ["2026-10-21", "2026-10-22"]
    assert len(unpack(summary, "workout_gps")) == 2
    assert unpack(summary, "workout_gps")[1]["timestamp"] == "invalid"


def test_sqlite_composition_archive_matches_importer_without_mutating_database(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE body_measurements (timestamp TEXT, weight_kg REAL, muscle_mass_kg REAL, water_pct REAL, user_id TEXT)")
        db.execute("INSERT INTO body_measurements VALUES ('2026-10-21T08:00:00',70,51.12345,56.5,'PRIVATE_SENTINEL')")
        db.execute("INSERT INTO body_measurements VALUES ('2026-10-21T20:00:00',71,NULL,NULL,'PRIVATE_SENTINEL')")
    before = path.read_bytes()
    py = dashboard_data.build_summary(path)
    js = run_document(document({"body_measurements": unpack(py, "body_measurements")}))
    assert js["record_details"] == py["record_details"]
    assert js["days"] == py["days"]
    assert path.read_bytes() == before
    assert "PRIVATE_SENTINEL" not in json.dumps(py)


@pytest.mark.parametrize("value", [101, -1, None, True, "invalid"])
def test_invalid_composition_does_not_become_valid_trend(value):
    summary = run_document(document({"body_measurements": [
        {"timestamp": "2026-10-21", "water_pct": value, "weight_kg": 70},
    ]}))
    assert summary["days"][0]["water_pct"] is None


def test_raw_and_summary_archives_enforce_limits_without_partial_success():
    source = document({"body_measurements": [
        {"timestamp": "2026-10-21", "weight_kg": 70},
        {"timestamp": "2026-10-21", "weight_kg": 71},
    ]})
    summary = run_document(source)
    for payload in (source, summary):
        result = run_cases([{"text": json.dumps(payload), "recordLimit": 1}])[0]["final"]
        assert result["type"] == "error"
        assert "规模过大" in result["message"]
        assert "data" not in result


def test_summary_archive_strips_unknown_columns_and_rejects_corrupt_rows():
    summary = run_document(document({"body_measurements": [
        {"timestamp": "2026-10-21", "weight_kg": 70},
    ]}))
    dataset = summary["record_details"]["datasets"]["body_measurements"]
    dataset["fields"].append("user_id")
    dataset["rows"][0].append("PRIVATE_SENTINEL")
    assert "PRIVATE_SENTINEL" not in json.dumps(run_document(summary))
    dataset["rows"][0].pop()
    result = run_cases([{"text": json.dumps(summary)}])[0]["final"]
    assert result["type"] == "error"


def test_late_columns_and_nested_stages_share_the_cell_budget():
    records = {"body_measurements": [
        {"timestamp": "2026-10-21", "weight_kg": 70},
        {"timestamp": "2026-10-21", "weight_kg": 71},
        {"timestamp": "2026-10-21", "weight_kg": 72, "bmi": 23, "water_pct": 56},
    ]}
    stages = {"sleep": [{"end_at": "2026-10-21", "stages": [
        {"stage": "light", "minutes": 10} for _ in range(4)
    ]}]}
    for payload in (document(records), document(stages)):
        result = run_cases([{"text": json.dumps(payload), "cellLimit": 6}])[0]["final"]
        assert result["type"] == "error"
        assert "规模过大" in result["message"]
