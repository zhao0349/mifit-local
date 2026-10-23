"""Repeatable synthetic-only worker checks; no real health paths are accepted.

The optional 282 MiB virtual stress case runs only with
MIFIT_RUN_WORKER_STRESS=1. Node.js must be on PATH; otherwise these tests skip.
"""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

import dashboard_data

HARNESS = Path(__file__).with_name("dashboard_worker_harness.cjs")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for synthetic worker tests")


def run_cases(cases, timeout=30):
    process = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps(cases, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=True,
    )
    return json.loads(process.stdout)


def document(records, **extra):
    return {"schema_version": "1.0", "records": records, **extra}


def run_document(value, chunk=7):
    result = run_cases([{"text": json.dumps(value, ensure_ascii=False), "chunkBytes": chunk}])[0]
    assert result["final"]["type"] == "complete", result["final"]
    return result["final"]["data"]


def day(summary, key="2026-10-21"):
    return next(item for item in summary["days"] if item["date"] == key)


def test_chunks_unicode_escapes_structure_and_identity_filtering():
    records = {
        "daily_activity": [{
            "date": "2026-10-20", "steps": 8000, "distance_m": 5000,
            "user_id": "synthetic-private-account", "device_id": "synthetic-private-device",
        }],
        "heart_rate": [{"timestamp": "2026-10-20T23:30:00Z", "bpm": 60, "sample_type": "resting"}],
        "workout_gps": [{"latitude": 12.345678, "longitude": 23.456789,
                         "nested": [{"text": 'records { } [ ] \\" 中文 😀'}]}],
        "workout_detail": [{"marker": "synthetic-private-detail"}],
    }
    value = document(records, metadata='not a structure: "records": {"heart_rate": []}')
    text = json.dumps(value, ensure_ascii=False)
    cases = [{"text": text, "chunkBytes": size} for size in (1, 2, 3, 7, 31, 262144)]
    for result in run_cases(cases):
        assert result["final"]["type"] == "complete"
        data = result["final"]["data"]
        assert day(data, "2026-10-20")["steps"] == 8000
        assert day(data)["heart_rate_avg"] == day(data)["resting_hr"] == 60
        assert data["meta"]["source_counts"]["workout_gps"] == 1
        assert data["meta"]["source_counts"]["workout_detail"] == 1
        exported = json.dumps(data)
        for forbidden in ("synthetic-private", '"user_id"', '"device_id"'):
            assert forbidden not in exported
        assert data["record_details"]["datasets"]["workout_gps"]["rows"] == [[12.345678, 23.456789]]
        assert result["maxRead"] <= 262144
        percentages = [item["percent"] for item in result["progress"]]
        assert percentages == sorted(percentages)
        assert percentages[-1] == 100


@pytest.mark.parametrize("bad_value", ["01", "-01", "+1", ".5", "1.", "1e", "1e+", "NaN",
                                      "Infinity", "truefalse", '"\\q"', '"\\uZZZZ"', '"line\nbreak"'])
def test_invalid_json_tokens_are_rejected_even_in_skipped_records(bad_value):
    text = '{"schema_version":"1.0","records":{"workout_gps":[{"x":' + bad_value + "}]}}"
    result = run_cases([{"text": text, "chunkBytes": 1}])[0]["final"]
    assert result["type"] == "error"
    assert bad_value not in result["message"]


def test_truncation_trailing_garbage_and_unexpected_structure():
    valid = '{"schema_version":"1.0","records":{"heart_rate":[{"timestamp":"2026-10-21","bpm":60}]}}'
    bad = [valid[:length] for length in range(1, len(valid))]
    bad += [
        valid + "x",
        '{"schema_version":"1.0","records":{"heart_rate":[{},]}}',
        '{"schema_version":"1.0","records":{"heart_rate":[],}}',
        '{"schema_version":"1.0","records":{"heart_rate":[1]}}',
        '{"schema_version":"1.0","records":"{}"}',
        '{"schema_version":"1.0","records":{},"schema_version":"1.0"}',
        '{"schema_version":"1.0","records":{"heart_rate":[],"heart_rate":[]}}',
        "[]", "{}", '{"schema_version":2,"days":[],"workouts":[]}',
    ]
    outputs = run_cases([{"text": text, "chunkBytes": 3} for text in bad])
    assert all(result["final"]["type"] == "error" for result in outputs)


def test_valid_exponents_and_literals_in_skipped_nested_objects():
    text = (
        '{"schema_version":"1.0","records":{"workout_gps":[{"x":'
        '[-0,1.25,1e-2,1E+3,1e400,true,false,null,{"escaped":"\\u4e2d\\ud83d\\ude00"}]'
        "}]}}"
    )
    final = run_cases([{"text": text, "chunkBytes": 1}])[0]["final"]
    assert final["type"] == "complete"
    assert final["data"]["meta"]["source_counts"]["workout_gps"] == 1


def test_file_extension_limit_and_invalid_utf8():
    text = json.dumps(document({}))
    cases = [{"text": text, "name": name} for name in ("synthetic.csv", "synthetic.zip")]
    cases += [
        {"text": text, "artificialSize": 1024**3 + 1},
        {"bytesBase64": base64.b64encode(b'{"schema_version":"1.0","x":"\xff"}').decode()},
        {"bytesBase64": base64.b64encode(b'{"x":"\xe4\xb8').decode(), "chunkBytes": 1},
    ]
    assert all(item["final"]["type"] == "error" for item in run_cases(cases))
    assert run_cases([{"text": text, "name": "SYNTHETIC.JSON"}])[0]["final"]["type"] == "complete"


def test_summary_round_trip_keeps_only_whitelisted_fields():
    source = run_document(document({
        "daily_activity": [{"date": "2026-10-21", "steps": 0}],
        "workouts": [{"start_at": "2026-10-21T18:00:00", "activity_type": "running"}],
    }))
    source["user_id"] = "synthetic-private-account"
    source["meta"]["notes"] = ["synthetic-private-notes"]
    source["days"][0]["device_id"] = "synthetic-private-device"
    source["workouts"][0]["gps"] = "synthetic-private-gps"
    imported = run_document(source)
    assert imported["days"] == [
        {key: value for key, value in source["days"][0].items() if key != "device_id"}
    ]
    assert imported["workouts"] == [
        {key: value for key, value in source["workouts"][0].items() if key != "gps"}
    ]
    assert "synthetic-private" not in json.dumps(imported)


def test_numeric_nulls_zero_ranges_and_python_rounding():
    values = [None, True, False, "", " ", "invalid", -1, 0, "0", 61.125, 61.375, 2.675, 1e200]
    records = {
        "heart_rate": [
            {"timestamp": f"2026-10-{index + 1:02}T08:00:00", "bpm": value, "sample_type": "resting"}
            for index, value in enumerate(values)
        ],
        "spo2": [{"timestamp": "2026-10-21", "spo2_pct": value} for value in (0, 101, 98, None)],
        "stress": [{"timestamp": "2026-10-21", "stress_score": value} for value in (-1, 101, 0, None)],
    }
    summary = run_document(document(records))
    for index, value in enumerate(values):
        expected = dashboard_data._number(value, positive=True)
        expected = round(expected, 2) if expected is not None else None
        row = day(summary, f"2026-10-{index + 1:02}")
        assert row["heart_rate_avg"] == row["resting_hr"] == expected
    assert day(summary)["spo2_avg"] == 98
    assert day(summary)["stress_avg"] == 0


def test_js_and_python_aggregation_semantics_match():
    datasets = {
        "daily_activity": [
            {"date": "2026-10-21", "steps": 5000, "distance_m": 3000, "active_kcal": 100},
            {"date": "2026-10-21", "steps": 6000, "distance_m": 4000,
             "collected_at": "2026-10-21T20:00:00.000001+08:00"},
            {"date": "2026-10-21", "steps": 6000, "distance_m": 4200,
             "collected_at": "2026-10-21T20:00:00.000002+08:00"},
            {"date": "2026-02-31", "steps": 1},
        ],
        "sleep": [
            {"start_at": "2026-10-20T23:00:00+08:00", "end_at": "2026-10-21T07:00:00+08:00",
             "duration_minutes": 480, "time_asleep_minutes": 450, "sleep_score": 85, "is_nap": 0,
             "stages": json.dumps([{"stage": "deep", "minutes": 80}, {"stage": "light", "minutes": 300}])},
            {"start_at": "2026-10-21T00:00:00", "end_at": "2026-10-21T06:00:00",
             "duration_minutes": 360, "time_asleep_minutes": 340, "sleep_score": 70, "is_nap": 0},
            {"start_at": "2026-10-21T13:00:00", "end_at": "2026-10-21T13:35:00",
             "duration_minutes": 35, "time_asleep_minutes": 30, "is_nap": 1},
            {"start_at": "2026-10-21T13:05:00", "end_at": "2026-10-21T13:30:00",
             "duration_minutes": 25, "time_asleep_minutes": 20, "is_nap": 1},
            {"start_at": "2026-10-21T16:00:00", "end_at": "2026-10-21T16:25:00",
             "duration_minutes": 25, "time_asleep_minutes": 20, "is_nap": 1},
        ],
        "heart_rate": [
            {"timestamp": "2026-10-20T15:59:59Z", "bpm": 90, "sample_type": "resting"},
            {"timestamp": "2026-10-20T16:00:00Z", "bpm": 60, "sample_type": "resting"},
            {"timestamp": "2026-10-21T23:59:59+08:00", "bpm": 80},
            {"timestamp": "2026-10-21T08:00:00", "bpm": 0, "sample_type": "resting"},
        ],
        "spo2": [{"timestamp": "2026-10-21", "spo2_pct": value} for value in (0, 98, 100)],
        "stress": [{"timestamp": "2026-10-21", "stress_score": value} for value in (0, 20)],
        "body_measurements": [
            {"timestamp": "2026-10-21T08:00:00.000001", "weight_kg": 70, "body_fat_pct": 20},
            {"timestamp": "2026-10-21T08:00:00.000002", "weight_kg": 71},
        ],
        "workouts": [
            {"start_at": "2026-10-20T16:30:00Z", "activity_type": "running", "duration_minutes": 30,
             "distance_m": 5000, "calories_kcal": 250, "avg_heart_rate_bpm": 140},
            {"start_at": "2026-10-21T18:00:00", "activity_type": "strength", "avg_heart_rate_bpm": 0},
        ],
    }
    js = run_document(document(datasets), chunk=11)
    tables = {
        "sleep": "sleep_sessions", "heart_rate": "heart_rate_samples",
        "spo2": "spo2_samples", "stress": "stress_samples",
    }
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(dashboard_data._DEMO_SCHEMA)
        for source, rows in datasets.items():
            table = tables.get(source, source)
            for record in rows:
                columns = ", ".join(f'"{name}"' for name in record)
                placeholders = ",".join("?" for _ in record)
                connection.execute(
                    f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(record.values())
                )
        py = dashboard_data._summarize(connection)
    assert js["days"] == py["days"]
    assert js["workouts"] == py["workouts"]
    assert js["meta"]["range"] == py["meta"]["range"]
    assert js["meta"]["source_counts"] == py["meta"]["source_counts"]


@pytest.mark.skipif(
    os.environ.get("MIFIT_RUN_WORKER_STRESS") != "1",
    reason="Optional 282 MiB virtual stress test; set MIFIT_RUN_WORKER_STRESS=1",
)
def test_large_virtual_gps_file_refuses_to_silently_truncate_details():
    result = run_cases([{"stressMiB": 282}], timeout=120)[0]
    assert result["final"]["type"] == "error"
    assert "规模过大" in result["final"]["message"]
    assert result["maxRead"] <= 262144
    assert result["reads"] > 1
