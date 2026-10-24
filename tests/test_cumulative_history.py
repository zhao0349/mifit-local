"""Cumulative history, replacement failure safety, and account isolation."""

import asyncio
import json
from pathlib import Path

import pytest
from mi_fitness_mcp.models import DailyActivity
from mi_fitness_mcp.storage import Database

import desktop_backend as backend
from test_desktop_backend import FakeAdapter
from test_desktop_backend import isolated as isolated


def activity(day, steps, user="123456"):
    return DailyActivity(id=f"day-{day}", date=day, steps=steps, distance_m=steps * 0.7, active_kcal=steps / 20,
                         provider="mi_fitness", source_type="cloud_session", user_id=user)


def cache(user="123456", region="cn"):
    backend.save_credentials(user, "synthetic-token", region)
    path = backend.DATA_DIR / backend.account_scope() / "mi_fitness.db"
    return path, Database(path)


def exported(result):
    return json.loads((Path(result["directory"]) / "mi_fitness.json").read_text(encoding="utf-8"))


def test_default_export_updates_one_archive_with_all_history_without_duplicates(isolated):
    path, db = cache()
    db.insert_daily_activity(activity("2026-01-01", 100))
    first = backend.export("2026-01-01", "2026-01-01", lambda _: None)
    legacy = Path(first["directory"]).parent / "legacy-export"
    legacy.mkdir()
    (legacy / "original.txt").write_text("preserve", encoding="utf-8")
    db.insert_daily_activity(activity("2026-01-01", 200))
    db.insert_daily_activity(activity("2026-01-02", 300))
    before = path.read_bytes()
    second = backend.export("2026-01-02", "2026-01-02", lambda _: None)
    again = backend.export("2026-01-02", "2026-01-02", lambda _: None)
    assert first["directory"] == second["directory"] == again["directory"]
    assert Path(second["directory"]).name == "current"
    rows = exported(second)["records"]["daily_activity"]
    assert [(row["date"], row["steps"]) for row in rows] == [("2026-01-01", 200), ("2026-01-02", 300)]
    assert second["total_rows"] == 2
    assert all(Path(name).is_file() for name in second["files"])
    assert exported(second)["filters"]["scope"] == "all_cached"
    assert exported(second)["filters"]["start_date"] is None
    assert path.read_bytes() == before
    assert {child.name for child in Path(second["directory"]).parent.iterdir()} == {"current", "legacy-export"}
    assert (legacy / "original.txt").read_text(encoding="utf-8") == "preserve"


def test_explicit_snapshot_remains_date_filtered_and_does_not_change_cumulative(isolated):
    _, db = cache()
    for day in (1, 2):
        db.insert_daily_activity(activity(f"2026-01-0{day}", day))
    current = backend.export("2026-01-02", "2026-01-02", lambda _: None)
    original = exported(current)
    snapshot = backend.export("2026-01-02", "2026-01-02", lambda _: None, mode="snapshot")
    assert snapshot["directory"] != current["directory"]
    assert snapshot["total_rows"] == 1
    assert exported(current) == original


def test_accounts_and_regions_have_separate_cumulative_archives(isolated):
    results = []
    for user, region, steps in (("123456", "cn", 100), ("678901", "cn", 200), ("123456", "us", 300)):
        _, db = cache(user, region)
        db.insert_daily_activity(activity("2026-01-01", steps, user))
        results.append(backend.export("2026-01-01", "2026-01-01", lambda _: None))
    assert len({result["directory"] for result in results}) == 3
    for result, steps in zip(results, (100, 200, 300), strict=True):
        assert [row["steps"] for row in exported(result)["records"]["daily_activity"]] == [steps]
    backend.save_credentials("123456", "synthetic-token", "cn")
    assert backend.export("2026-02-01", "2026-02-01", lambda _: None)["directory"] == results[0]["directory"]


def test_failed_publish_rolls_back_complete_prior_json_and_csv(isolated, monkeypatch):
    _, db = cache()
    db.insert_daily_activity(activity("2026-01-01", 100))
    first = backend.export("2026-01-01", "2026-01-01", lambda _: None)
    before = {name: Path(name).read_bytes() for name in first["files"]}
    db.insert_daily_activity(activity("2026-01-02", 200))
    original_replace = Path.replace

    def fail_publish(self, target):
        if self.name.startswith(".export-") and Path(target).name == "current":
            raise OSError("synthetic publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_publish)
    with pytest.raises(backend.DesktopError):
        backend.export("2026-01-02", "2026-01-02", lambda _: None)
    assert all(Path(name).read_bytes() == content for name, content in before.items())
    assert {child.name for child in Path(first["directory"]).parent.iterdir()} == {"current"}


def test_interrupted_directory_swap_is_recovered_before_next_build(isolated, monkeypatch):
    _, db = cache()
    db.insert_daily_activity(activity("2026-01-01", 100))
    first = backend.export("2026-01-01", "2026-01-01", lambda _: None)
    current = Path(first["directory"])
    current.replace(current.with_name(".current-previous"))
    def fail(*args):
        raise OSError("synthetic build failure")
    monkeypatch.setattr(backend, "_export_database", fail)
    with pytest.raises(backend.DesktopError):
        backend.export("2026-01-01", "2026-01-01", lambda _: None)
    assert exported(first)["records"]["daily_activity"][0]["steps"] == 100


class RecordAdapter(FakeAdapter):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def iter_daily_activity(self, start, end):
        for row in self.rows:
            if start <= row.date <= end:
                yield row
        if self.fail:
            raise RuntimeError("synthetic failure after some records")


@pytest.mark.asyncio
async def test_sync_skips_verified_days_and_retains_unrequested_days(isolated, monkeypatch):
    path, db = cache()
    db.insert_daily_activity(activity("2025-12-01", 10))
    adapter = RecordAdapter([activity("2026-01-01", 100)])
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    first = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    adapter.rows = [activity("2026-01-01", 200)]
    second = await backend.sync("2026-01-01", "2026-01-01", lambda _: None)
    assert first["added"] == 1 and second["updated"] == 0
    assert second["skipped_days"] == 1
    rows = Database(path).query_daily_activity("123456", "2025-01-01", "2026-12-31")
    assert [(row["date"], row["steps"]) for row in rows] == [("2025-12-01", 10), ("2026-01-01", 100)]


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_replacement_publishes_only_complete_sync_and_preserves_other_account(isolated, monkeypatch, fails):
    other, other_db = cache("678901")
    other_db.insert_daily_activity(activity("2025-12-01", 99, "678901"))
    other_before = other.read_bytes()
    path, db = cache()
    db.insert_daily_activity(activity("2025-12-01", 10))
    before = path.read_bytes()
    adapter = RecordAdapter([activity("2026-01-01", 200)])
    adapter.fail = fails
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    result = await backend.sync("2026-01-01", "2026-01-01", lambda _: None,
                                mode="replace", expected_account=backend.account_scope())
    assert result["committed"] is (not fails)
    if fails:
        assert path.read_bytes() == before
    else:
        rows = Database(path).query_daily_activity("123456", "2025-01-01", "2026-12-31")
        assert [row["date"] for row in rows] == ["2026-01-01"]
    assert other.read_bytes() == other_before
    assert not list(path.parent.glob(".replace-*"))


@pytest.mark.asyncio
async def test_cancel_during_replacement_cleanup_keeps_old_history(isolated, monkeypatch):
    path, db = cache()
    db.insert_daily_activity(activity("2025-12-01", 10))
    before = path.read_bytes()
    adapter = RecordAdapter([activity("2026-01-01", 200)])
    cleaning = asyncio.Event()
    async def close():
        cleaning.set()
        await asyncio.sleep(60)
    adapter.close = close
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    task = asyncio.create_task(backend.sync("2026-01-01", "2026-01-01", lambda _: None,
                                           mode="replace", expected_account=backend.account_scope()))
    await cleaning.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert path.read_bytes() == before
    assert not backend._operation_lock.locked()
    assert not list(path.parent.glob(".replace-*"))


@pytest.mark.asyncio
async def test_stale_account_confirmation_cannot_replace_new_account(isolated):
    _, _ = cache()
    old_scope = backend.account_scope()
    other, _ = cache("678901")
    before = other.read_bytes()
    with pytest.raises(backend.DesktopError, match="账号已变化"):
        await backend.sync("2026-01-01", "2026-01-01", lambda _: None,
                           mode="replace", expected_account=old_scope)
    assert other.read_bytes() == before
    assert not backend._operation_lock.locked()
