"""Completed-day sync coverage, exercised through synthetic cloud adapters."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from mi_fitness_mcp.models import Workout, WorkoutSample
from mi_fitness_mcp.storage import Database

import desktop_backend as backend
from test_cumulative_history import RecordAdapter, activity, cache
from test_desktop_backend import isolated as isolated


class TrackingAdapter(RecordAdapter):
    def __init__(self, rows=()):
        super().__init__(rows)
        self.calls = []

    async def iter_daily_activity(self, start, end):
        self.calls.append(("daily_activity", start, end))
        async for row in super().iter_daily_activity(start, end):
            yield row

    async def iter_heart_rate(self, start, end):
        self.calls.append(("heart_rate", start, end))
        if False:
            yield


@pytest.fixture
def sync_adapter(isolated, monkeypatch):
    backend.save_credentials("123456", "synthetic-token")
    adapter = TrackingAdapter()
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    return adapter


@pytest.mark.asyncio
async def test_completed_empty_days_and_no_workouts_need_no_cloud_connection(sync_adapter, monkeypatch):
    monkeypatch.setattr(backend, "DATA_TYPES", {
        "daily_activity": "日常活动", "workout_detail": "运动明细及轨迹",
    })
    first = await backend.sync("2026-01-01", "2026-01-03", lambda _: None)
    assert first["all_ok"] and first["added"] == 0
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: pytest.fail("cached days contacted cloud"))
    second = await backend.sync("2026-01-01", "2026-01-03", lambda _: None)
    assert second["all_ok"]
    assert sync_adapter.calls == [("daily_activity", "2026-01-01", "2026-01-03")]


@pytest.mark.asyncio
async def test_overlapping_range_fetches_only_holes_in_seven_day_chunks(sync_adapter):
    for day in ("2026-01-02", "2026-01-04"):
        assert (await backend.sync(day, day, lambda _: None))["all_ok"]
    sync_adapter.calls.clear()
    result = await backend.sync("2026-01-01", "2026-01-16", lambda _: None)
    assert result["all_ok"]
    assert sync_adapter.calls == [
        ("daily_activity", "2026-01-01", "2026-01-01"),
        ("daily_activity", "2026-01-03", "2026-01-03"),
        ("daily_activity", "2026-01-05", "2026-01-11"),
        ("daily_activity", "2026-01-12", "2026-01-16"),
    ]


@pytest.mark.asyncio
async def test_coverage_is_separate_for_each_data_type_and_account(sync_adapter, monkeypatch):
    await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动", "heart_rate": "心率"})
    sync_adapter.calls.clear()
    assert (await backend.sync("2026-01-01", "2026-01-01", lambda _: None))["all_ok"]
    assert sync_adapter.calls == [("heart_rate", "2026-01-01", "2026-01-01")]
    backend.save_credentials("678901", "other-synthetic-token")
    sync_adapter.user_id = "678901"
    sync_adapter.calls.clear()
    assert (await backend.sync("2026-01-01", "2026-01-01", lambda _: None))["all_ok"]
    assert sync_adapter.calls == [
        ("daily_activity", "2026-01-01", "2026-01-01"),
        ("heart_rate", "2026-01-01", "2026-01-01"),
    ]


@pytest.mark.asyncio
async def test_partial_records_and_bad_records_do_not_mark_a_day_complete(sync_adapter, monkeypatch):
    async def incomplete(start, end):
        sync_adapter.calls.append(("daily_activity", start, end))
        if start == "2026-01-01":
            yield activity(start, 100)
            raise RuntimeError("synthetic interrupted response")
        if start == "2026-01-08":
            yield activity(start, 100, user="678901")

    monkeypatch.setattr(sync_adapter, "iter_daily_activity", incomplete)
    first = await backend.sync("2026-01-01", "2026-01-15", lambda _: None)
    assert not first["all_ok"]
    assert first["types"]["daily_activity"]["failed_chunks"] == 2
    monkeypatch.setattr(sync_adapter, "iter_daily_activity", TrackingAdapter.iter_daily_activity.__get__(sync_adapter))
    sync_adapter.calls.clear()
    second = await backend.sync("2026-01-01", "2026-01-15", lambda _: None)
    assert second["all_ok"]
    assert sync_adapter.calls == [
        ("daily_activity", "2026-01-01", "2026-01-07"),
        ("daily_activity", "2026-01-08", "2026-01-14"),
    ]


@pytest.mark.asyncio
async def test_cancellation_keeps_completed_chunks_and_retries_unfinished_dates(sync_adapter, monkeypatch):
    entered = asyncio.Event()

    async def blocked(start, end):
        sync_adapter.calls.append(("daily_activity", start, end))
        if start == "2026-01-08":
            entered.set()
            await asyncio.Event().wait()
        if False:
            yield

    monkeypatch.setattr(sync_adapter, "iter_daily_activity", blocked)
    task = asyncio.create_task(backend.sync("2026-01-01", "2026-01-15", lambda _: None))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(sync_adapter, "iter_daily_activity", TrackingAdapter.iter_daily_activity.__get__(sync_adapter))
    sync_adapter.calls.clear()
    assert (await backend.sync("2026-01-01", "2026-01-15", lambda _: None))["all_ok"]
    assert sync_adapter.calls == [
        ("daily_activity", "2026-01-08", "2026-01-14"),
        ("daily_activity", "2026-01-15", "2026-01-15"),
    ]


@pytest.mark.asyncio
async def test_today_and_future_dates_remain_refreshable(sync_adapter):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    await backend.sync(yesterday, tomorrow, lambda _: None)
    sync_adapter.calls.clear()
    assert (await backend.sync(yesterday, tomorrow, lambda _: None))["all_ok"]
    assert sync_adapter.calls == [("daily_activity", today.isoformat(), tomorrow)]


@pytest.mark.asyncio
async def test_legacy_rows_are_checked_once_and_explicit_replacement_refetches(sync_adapter):
    path, database = cache()
    database.insert_daily_activity(activity("2025-12-01", 10))
    database.insert_daily_activity(activity("2026-01-01", 100))
    sync_adapter.rows = [activity("2026-01-01", 200)]
    first = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    assert first["updated"] == 1
    sync_adapter.calls.clear()
    sync_adapter.rows = [activity("2026-01-01", 300)]
    repeated = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    assert repeated["updated"] == 0 and sync_adapter.calls == []
    replacement = await backend.sync(
        "2026-01-01", "2026-01-01", lambda _: None,
        mode="replace", expected_account=backend.account_scope(),
    )
    assert replacement["all_ok"] and replacement["committed"]
    assert sync_adapter.calls == [("daily_activity", "2026-01-01", "2026-01-01")]
    rows = Database(path).query_daily_activity("123456", "2025-01-01", "2026-12-31")
    assert [(row["date"], row["steps"]) for row in rows] == [("2026-01-01", 300)]


@pytest.mark.asyncio
async def test_workout_details_skip_cached_and_outside_range_but_fetch_missing(sync_adapter, monkeypatch):
    _, database = cache()
    common = {"provider": "mi_fitness", "source_type": "cloud_session", "user_id": "123456"}

    def workout(identifier, day):
        start = datetime.fromisoformat(day + "T09:00:00+08:00")
        return Workout(
            **common, id=identifier, workout_id=identifier, activity_type="running",
            start_at=start, end_at=start + timedelta(minutes=10), duration_minutes=10,
            fds_sid="synthetic-fds", report_time=int(start.timestamp()),
        )

    def sample(identifier, timestamp):
        return WorkoutSample(
            **common, id=identifier + "-sample", workout_id=identifier,
            timestamp=timestamp, offset_seconds=0, heart_rate_bpm=100,
        )

    cached = workout("cached", "2026-01-01")
    database.insert_workout(cached)
    database.insert_workout_detail_samples([sample("cached", cached.start_at)])
    database.insert_workout(workout("outside", "2026-01-03"))
    monkeypatch.setattr(backend, "DATA_TYPES", {"workout_detail": "运动明细及轨迹"})
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: pytest.fail("cached workout contacted cloud"))
    first = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    assert first["all_ok"] and first["skipped"] == 1

    database.insert_workout(workout("missing", "2026-01-01"))
    downloaded = []

    async def fetch_detail(row):
        downloaded.append(row["workout_id"])
        return [sample(row["workout_id"], datetime.fromisoformat(row["start_at"]))], []

    monkeypatch.setattr(sync_adapter, "fetch_workout_detail", fetch_detail, raising=False)
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: sync_adapter)
    second = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    assert second["all_ok"] and second["added"] == 1 and second["skipped"] == 1
    assert downloaded == ["missing"]
    assert database.workout_detail_counts("123456", "missing") == (1, 0)
