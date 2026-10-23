"""Synthetic-only tests for the browser's pure detail data model."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for the pure JS model")


def analyze(days=(), workouts=(), *, options=None, options_list=None):
    payload = {
        "data": {"days": list(days), "workouts": list(workouts)},
        "optionsList": options_list or [options or {
            "category": "activity", "metric": "steps",
            "start": "2026-07-01", "end": "2026-07-03",
        }],
    }
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "dashboard_details_harness.cjs")],
        input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True,
        check=True, timeout=15,
    )
    return json.loads(result.stdout)


def test_categories_and_metric_contract():
    result = analyze()
    assert [item["id"] for item in result["categories"]] == [
        "activity", "sleep", "heart", "workouts", "body",
    ]
    assert result["metrics"]["steps"] == {
        "label": "步数", "unit": "步", "decimals": 0,
        "aggregation": "sum", "category": "activity",
    }
    assert result["metrics"]["sleep_minutes"]["aggregation"] == "mean"
    assert result["metrics"]["workout_count"]["label"] == "运动记录数"


def test_day_gaps_zero_and_daily_stats_do_not_impute_values():
    result = analyze([
        {"date": "2026-07-01", "steps": 0},
        {"date": "2026-07-03", "steps": 9000},
    ])["results"][0]
    assert [item["value"] for item in result["series"]] == [0, None, 9000]
    assert [item["count"] for item in result["series"]] == [1, 0, 1]
    assert result["stats"] == {
        "mean": 4500, "total": 9000, "min": 0, "max": 9000,
        "first": 0, "last": 9000, "delta": None, "previousMean": None,
        "coverage": pytest.approx(2 / 3), "calendarDays": 3,
    }
    assert result["days"][1]["steps"] is None
    assert result["days"][1]["workout_count"] is None
    assert result["days"][0]["workout_count"] == 0


def test_equal_calendar_length_previous_comparison_uses_valid_day_means():
    days = [
        {"date": "2026-06-27", "steps": 999999},
        {"date": "2026-06-28", "steps": 1000},
        {"date": "2026-06-30", "steps": 3000},
        {"date": "2026-07-01", "steps": 1000},
        {"date": "2026-07-03", "steps": 9000},
    ]
    stats = analyze(days)["results"][0]["stats"]
    assert stats["mean"] == 5000
    assert stats["previousMean"] == 2000
    assert stats["delta"] == 3000
    assert stats["first"] == 1000 and stats["last"] == 9000


def test_week_starts_monday_preserves_empty_weeks_and_does_not_change_stats():
    days = [
        {"date": "2026-06-28", "steps": 1000},
        {"date": "2026-06-29", "steps": 2000},
        {"date": "2026-07-01", "steps": 3000},
    ]
    base = {"category": "activity", "metric": "steps", "start": "2026-06-28", "end": "2026-07-12"}
    result = analyze(days, options_list=[
        {**base, "granularity": "day"}, {**base, "granularity": "week"},
    ])["results"]
    assert result[0]["stats"] == result[1]["stats"]
    assert [(item["date"], item["end"], item["value"], item["count"]) for item in result[1]["series"]] == [
        ("2026-06-22", "2026-06-28", 1000, 1),
        ("2026-06-29", "2026-07-05", 5000, 2),
        ("2026-07-06", "2026-07-12", None, 0),
    ]


def test_month_buckets_calendar_boundaries_and_mean_metrics():
    days = [
        {"date": "2024-02-28", "sleep_minutes": 360},
        {"date": "2024-02-29", "sleep_minutes": 480},
        {"date": "2024-03-01", "sleep_minutes": 0},
    ]
    result = analyze(days, options={
        "category": "sleep", "metric": "sleep_minutes", "granularity": "month",
        "start": "2024-02-28", "end": "2024-03-02",
    })["results"][0]
    assert [(item["date"], item["end"], item["value"], item["count"]) for item in result["series"]] == [
        ("2024-02-01", "2024-02-29", 420, 2),
        ("2024-03-01", "2024-03-31", 0, 1),
    ]
    assert result["stats"]["mean"] == 280
    assert result["stats"]["total"] is None


def test_sleep_stages_only_complete_shared_nights_and_zero_score_remains_valid():
    days = [
        {"date": "2026-07-01", "sleep_score": 0,
         "sleep_stages": {"deep": 60, "light": 300, "rem": 90, "awake": 0}},
        {"date": "2026-07-02", "sleep_score": 100,
         "sleep_stages": {"deep": 1000, "light": None, "rem": None, "awake": None}},
        {"date": "2026-07-03", "sleep_score": None,
         "sleep_stages": {"deep": 80, "light": 320, "rem": 70, "awake": 20}},
    ]
    result = analyze(days, options={
        "category": "sleep", "metric": "sleep_score",
        "start": "2026-07-01", "end": "2026-07-03",
    })["results"][0]
    assert result["stats"]["mean"] == 50
    assert result["sleepStages"] == {"count": 2, "deep": 70, "light": 310, "rem": 80, "awake": 10}


def test_workout_filter_drives_daily_values_sorted_rows_types_and_null_totals():
    days = [{"date": "2026-07-01"}, {"date": "2026-07-03"}]
    workouts = [
        {"date": "2026-07-01", "start": "09:00", "type": "running",
         "duration_minutes": None, "distance_km": None, "calories_kcal": None},
        {"date": "2026-07-03", "start": "08:00", "type": "walking",
         "duration_minutes": 20, "distance_km": 2, "calories_kcal": 100},
        {"date": "2026-07-01", "start": "18:00", "type": "running",
         "duration_minutes": None, "distance_km": 0, "calories_kcal": 0},
    ]
    base = {"category": "workouts", "start": "2026-07-01", "end": "2026-07-03", "workoutType": "running"}
    results = analyze(days, workouts, options_list=[
        {**base, "metric": "workout_count"}, {**base, "metric": "workout_minutes"},
    ])["results"]
    assert [item["value"] for item in results[0]["series"]] == [2, None, 0]
    assert results[0]["stats"]["total"] == 2
    assert results[0]["stats"]["coverage"] == pytest.approx(2 / 3)
    assert results[1]["stats"]["total"] is None
    assert results[1]["stats"]["mean"] is None
    assert [item["start"] for item in results[0]["workouts"]] == ["18:00", "09:00"]
    assert results[0]["workoutTypes"] == [
        {"type": "running", "count": 2, "minutes": None, "distance_km": 0, "kcal": 0},
    ]


def test_workouts_are_known_even_without_daily_rows_and_mixed_durations_sum_known_only():
    result = analyze([], [
        {"date": "2026-07-02", "start": "09:00", "type": "running", "duration_minutes": 0},
        {"date": "2026-07-02", "start": "17:00", "type": "running", "duration_minutes": 30},
        {"date": "2026-07-02", "start": "18:00", "type": "running", "duration_minutes": None},
    ], options={
        "category": "workouts", "metric": "workout_minutes",
        "start": "2026-07-01", "end": "2026-07-03",
    })["results"][0]
    assert [item["value"] for item in result["series"]] == [None, 30, None]
    assert result["stats"]["total"] == 30
    assert result["days"][1]["workout_count"] == 3


def test_weekday_uses_original_daily_values_and_no_missing_zero_fill():
    result = analyze([
        {"date": "2026-06-29", "steps": 1000},
        {"date": "2026-07-06", "steps": 9000},
    ], options={
        "category": "activity", "metric": "steps", "granularity": "week",
        "start": "2026-06-29", "end": "2026-07-12",
    })["results"][0]
    assert result["weekday"][0] == {"label": "周一", "count": 2, "mean": 5000}
    assert all(item["count"] == 0 and item["mean"] is None for item in result["weekday"][1:])


def test_missing_means_null_input_not_mutated_identifiers_not_propagated():
    payload = analyze([
        {"date": "2026-07-01", "steps": None, "user_id": "synthetic-private-id"},
    ], [{
        "date": "2026-07-01", "type": "running", "device_id": "synthetic-device",
        "latitude": 12.12345678,
    }])
    result = payload["results"][0]
    assert payload["inputUnchanged"]
    assert all(result["stats"][key] is None for key in ("mean", "total", "min", "max", "first", "last", "delta"))
    assert result["stats"]["coverage"] == 0
    assert result["sleepStages"] == {"count": 0, "deep": None, "light": None, "rem": None, "awake": None}
    text = json.dumps(result)
    assert "synthetic-private-id" not in text and "synthetic-device" not in text
    assert "12.12345678" not in text


def test_invalid_dates_numeric_values_and_duplicate_days_do_not_inflate_statistics():
    result = analyze([
        {"date": "2026-02-30", "steps": 999999},
        {"date": "2026-07-01", "steps": 500},
        {"date": "2026-07-01", "steps": 1000},
        {"date": "2026-07-02", "steps": "9000"},
        {"date": "2026-07-03", "steps": -20},
    ])["results"][0]
    assert result["stats"]["mean"] == 1000
    assert result["stats"]["total"] == 1000
    assert result["stats"]["coverage"] == pytest.approx(1 / 3)


def test_previous_mean_zero_is_real_and_not_used_as_a_false_missing_value():
    result = analyze([
        {"date": "2026-06-30", "stress_avg": 0},
        {"date": "2026-07-01", "stress_avg": 20},
    ], options={
        "category": "body", "metric": "stress_avg",
        "start": "2026-07-01", "end": "2026-07-01",
    })["results"][0]
    assert result["stats"]["previousMean"] == 0
    assert result["stats"]["delta"] == 20
    assert result["stats"]["total"] is None


def test_empty_data_without_dates_returns_empty_model():
    result = analyze(options={})["results"][0]
    # The helper supplies its default range for {}, so test the no-range path directly.
    assert result["stats"]["calendarDays"] == 3
    direct = subprocess.run(
        [NODE, str(ROOT / "tests" / "dashboard_details_harness.cjs")],
        input='{"data":{},"options":{}}', text=True, encoding="utf-8",
        capture_output=True, check=True, timeout=15,
    )
    empty = json.loads(direct.stdout)["results"][0]
    assert empty["days"] == [] and empty["series"] == [] and empty["workouts"] == []
    assert empty["stats"]["calendarDays"] == 0 and empty["stats"]["coverage"] == 0
