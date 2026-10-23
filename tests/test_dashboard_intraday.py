"""Synthetic intraday contracts, worker parity and safe timeline handling."""

import copy
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from test_dashboard_worker import document, run_cases, run_document

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("dashboard_intraday_harness.cjs")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def invoke(method, value):
    result = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps([{"method": method, "value": value}]),
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
    )
    return json.loads(result.stdout)[0]


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def session(**extra):
    value = {
        "start": epoch("2026-10-20T23:00:00+08:00"),
        "end": epoch("2026-10-21T07:00:00+08:00"),
        "nap": False, "asleep": 450, "score": 85,
        "stages": [["light", 120], ["deep", 90], ["light", 150], ["rem", 90]],
        "timing": "sequence",
    }
    return {**value, **extra}


def test_heart_offsets_beijing_milliseconds_order_and_simultaneous_points_preserved():
    summary = run_document(document({
        "heart_rate": [
            {"timestamp": "2026-10-21T08:00:00.123+08:00", "bpm": 80, "sample_type": "active"},
            {"timestamp": "2026-10-20T16:00:00Z", "bpm": 60, "sample_type": "passive"},
            {"timestamp": "2026-10-20T16:00:00Z", "bpm": 70, "sample_type": "resting"},
            {"timestamp": "2026-10-20T16:00:00Z", "bpm": 60, "sample_type": "passive"},
            {"timestamp": "2026-10-21T23:59:59.999", "bpm": 75},
            {"timestamp": "2026-10-20T15:59:59.999Z", "bpm": 65},
            {"timestamp": "2026-02-31", "bpm": 90},
            {"timestamp": "2026-10-21", "bpm": 0},
        ]
    }), chunk=3)
    heart = summary["intraday"]["heart"]
    assert heart["2026-10-20"] == [[86399999, 65, 0]]
    assert heart["2026-10-21"] == [
        [0, 60, 0], [0, 70, 1], [0, 60, 0], [28800123, 80, 0], [86399999, 75, 0],
    ]
    assert "raw_samples" not in summary["meta"]["omitted"]
    assert "gps" not in summary["meta"]["omitted"]


def test_sleep_source_sequence_all_sessions_exact_dedup_only_and_round_trip():
    primary = {
        "start_at": "2026-10-20T23:00:00+08:00", "end_at": "2026-10-21T07:00:00+08:00",
        "duration_minutes": 480, "time_asleep_minutes": 450, "sleep_score": 85,
        "is_nap": False,
        "stages": [{"stage": stage, "minutes": minutes} for stage, minutes in session()["stages"]],
    }
    different = {**primary, "sleep_score": 86}
    nap = {
        "start_at": "2026-10-21T12:00:00+08:00", "end_at": "2026-10-21T12:30:00+08:00",
        "duration_minutes": 30, "time_asleep_minutes": 20, "is_nap": 1,
        "stages": '[{"stage":"light","minutes":20}]',
    }
    summary = run_document(document({"sleep": [nap, primary, {**primary, "id": "synthetic-private"}, different]}))
    sessions = summary["intraday"]["sleep"]["2026-10-21"]
    assert len(sessions) == 3
    assert sessions[0] == session()
    assert sessions[1]["score"] == 86
    assert sessions[2]["nap"] is True
    assert sessions[2]["stages"] == [["light", 20]]
    assert "synthetic-private" not in json.dumps(summary)
    imported = run_document(summary)
    assert imported["intraday"] == summary["intraday"]
    assert imported["days"] == summary["days"]
    assert imported["workouts"] == summary["workouts"]


def test_model_whitelist_invalid_points_dates_and_no_input_mutation():
    value = {
        "version": 1, "user_id": "synthetic-private",
        "heart": {
            "2026-10-21": [[1000, 70, 1, "private"], [0, 60, 99], [-1, 80, 0],
                           [86400000, 80, 0], [0, 0, 0], [0, True, 0], ["0", 60, 0]],
            "2026-02-31": [[0, 80, 0]], "__proto__": [[0, 80, 0]],
        },
        "sleep": {"2026-10-21": [session(gps="synthetic-private")]},
    }
    before = copy.deepcopy(value)
    clean = invoke("normalize", value)["value"]
    assert clean == {
        "version": 1, "heart": {"2026-10-21": [[0, 60, 0], [1000, 70, 1]]},
        "sleep": {"2026-10-21": [session()]},
    }
    assert value == before
    assert invoke("normalize", None)["value"] == {"version": 1, "heart": {}, "sleep": {}}


def test_sleep_timeline_source_order_approximate_positions_and_unlocated_remainder():
    raw = session()
    result = invoke("sleepTimeline", raw)["value"]
    assert result["valid"] is True
    assert result["unplacedMinutes"] == 30
    assert [item["stage"] for item in result["segments"]] == ["light", "deep", "light", "rem"]
    assert result["segments"][0]["start"] == raw["start"]
    assert result["segments"][-1]["end"] == raw["end"] - 30 * 60000
    assert len(result["segments"]) == 4  # No invented final awake/unknown segment.
    assert "近似" in result["reason"] and "未知" in result["reason"]


@pytest.mark.parametrize("stages", [
    [["light", 481]],
    [["light", 1e308], ["deep", 1e308]],
])
def test_overfull_sleep_timeline_never_generates_false_clock_positions(stages):
    result = invoke("sleepTimeline", session(stages=stages))["value"]
    assert result["valid"] is False
    assert result["segments"] == []
    assert result["unplacedMinutes"] is None


def test_unknown_stage_preserves_duration_but_invalid_duration_disables_placement():
    unknown = session(stages=[["light", 10], ["source-private-label", 20], ["deep", 30]])
    normalized = invoke("normalize", {"version": 1, "sleep": {"2026-10-21": [unknown]}})["value"]
    cleaned = normalized["sleep"]["2026-10-21"][0]
    assert cleaned["stages"] == [["light", 10], ["unknown", 20], ["deep", 30]]
    assert "source-private-label" not in json.dumps(normalized)
    assert invoke("sleepTimeline", cleaned)["value"]["valid"] is True
    invalid = session(stages=[["light", 10], ["deep", None], ["rem", 30]])
    clean = invoke("normalize", {"version": 1, "sleep": {"2026-10-21": [invalid]}})["value"]["sleep"]["2026-10-21"][0]
    assert clean["sequenceValid"] is False
    assert clean["stages"] == [["light", 10], ["rem", 30]]
    assert invoke("sleepTimeline", clean)["value"]["valid"] is False
    assert invoke("sleepTimeline", clean)["value"]["segments"] == []


def test_worker_marks_invalid_stage_sequence_and_preserves_it_on_summary_import():
    raw = {
        "start_at": "2026-10-20T23:00:00+08:00", "end_at": "2026-10-21T07:00:00+08:00",
        "stages": [{"stage": "light", "minutes": 10}, {"stage": "deep", "minutes": None},
                   {"stage": "rem", "minutes": 30}],
    }
    summary = run_document(document({"sleep": [raw]}))
    row = summary["intraday"]["sleep"]["2026-10-21"][0]
    assert row["sequenceValid"] is False
    assert row["stages"] == [["light", 10], ["rem", 30]]
    assert run_document(summary)["intraday"] == summary["intraday"]


def test_bad_contract_and_oversized_sequences_fail_with_fixed_errors():
    assert invoke("normalize", {"version": 2})["name"] == "TypeError"
    assert invoke("normalize", {"version": 1, "heart": []})["name"] == "TypeError"
    too_many = {"version": 1, "heart": {"2026-10-21": [[0, 60, 0]] * 100001}}
    assert invoke("normalize", too_many)["name"] == "RangeError"
    stages = {"version": 1, "sleep": {"2026-10-21": [session(stages=[["light", 0]] * 10001)]}}
    assert invoke("normalize", stages)["name"] == "RangeError"
    summary = {"schema_version": 1, "days": [], "workouts": [], "intraday": {"version": 2}}
    result = run_cases([{"text": json.dumps(summary)}])[0]["final"]
    assert result["type"] == "error"


def test_empty_invalid_and_wrong_wake_day_sleep_handling():
    assert invoke("sleepTimeline", session(stages=[]))["value"] == {
        "valid": False, "segments": [], "unplacedMinutes": 480,
        "reason": "来源没有提供可用的阶段顺序。",
    }
    invalid = session(end=session()["start"])
    assert invoke("sleepTimeline", invalid)["value"]["valid"] is False
    assert invoke("normalize", {
        "version": 1, "sleep": {"2026-10-20": [session()], "2026-10-21": [invalid]},
    })["value"]["sleep"] == {}
