"""Synthetic-only desktop tests: no real credential or health-data access."""

import asyncio
import csv
import json
import sqlite3
from datetime import datetime

import pytest

import desktop_backend as backend


class FakeVault:
    def __init__(self):
        self.values = {}
        self.fail_key = None

    def get_password(self, service, key):
        assert service == backend.CREDENTIAL_SERVICE
        return self.values.get(key)

    def set_password(self, service, key, value):
        assert service == backend.CREDENTIAL_SERVICE
        if key == self.fail_key:
            raise RuntimeError("secret-response-must-never-escape")
        self.values[key] = value

    def delete_password(self, service, key):
        self.values.pop(key, None)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name, value in {
        "ROOT": tmp_path, "DATA_DIR": tmp_path / "data",
        "EXPORTS_DIR": tmp_path / "exports", "DEMO_DIR": tmp_path / "demo",
        "SETTINGS_PATH": tmp_path / "data" / "settings.json",
    }.items():
        monkeypatch.setattr(backend, name, value)
    vault = FakeVault()
    monkeypatch.setattr(backend, "_vault", lambda: vault)
    return vault


@pytest.mark.parametrize("user,token", [
    ("1\r\nCookie:evil", "safe"), (" 123", "safe"), ("abc", "safe"),
    ("123", "foo;evil=1"), ("123", "foo\r\nbar"), ("123", "foo\nbar"),
    ("123", "foo bar"), ("123", '"foo"'), ("123", "中文"),
    ("123", "foo\\bar"), ("123", "foo,bar"), ("123", ""),
])
def test_credentials_reject_header_injection_without_writing(isolated, user, token):
    with pytest.raises(ValueError):
        backend.save_credentials(user, token)
    assert isolated.values == {}
    assert not backend.SETTINGS_PATH.exists()


def test_account_isolation_and_status_do_not_reveal_credentials(isolated):
    backend.save_credentials("123456", "first-fake-token")
    first = backend._settings()["account_key"]
    status = backend.credentials_status()
    assert status == {
        "configured": True, "keyring_available": True, "account_masked": "******56",
    }
    backend.save_credentials("678901", "second-fake-token")
    assert first != backend._settings()["account_key"]
    text = backend.SETTINGS_PATH.read_text(encoding="utf-8")
    assert "678901" not in text and "token" not in text
    assert "Asia/Shanghai" in text and '"cn"' in text


def test_failed_save_does_not_mix_old_user_and_new_token(isolated):
    backend.save_credentials("123456", "first-fake-token")
    isolated.fail_key = "mi_fitness_auth_user_id"
    with pytest.raises(backend.DesktopError) as error:
        backend.save_credentials("678901", "second-fake-token")
    assert "secret-response" not in str(error.value)
    assert not backend.credentials_status()["configured"]


@pytest.mark.parametrize("start,end", [
    ("2026-7-15", "2026-07-15"), ("2026-02-30", "2026-03-01"),
    ("2026-07-16", "2026-07-15"), ("2026-07-15\n", "2026-07-15"),
])
def test_date_validation(start, end):
    with pytest.raises(ValueError):
        backend.validate_dates(start, end)


def test_chunk_boundaries():
    assert list(backend._chunks("2026-07-01", "2026-07-15")) == [
        ("2026-07-01", "2026-07-07"), ("2026-07-08", "2026-07-14"),
        ("2026-07-15", "2026-07-15"),
    ]


class FakeAdapter:
    def __init__(self):
        self.closed = False
        self.user_id = "123456"
        self.block = False
        self.fail = False
        self.started = asyncio.Event()

    async def connect(self):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return True

    def get_user_id(self):
        return self.user_id

    def is_connected(self):
        return True

    async def close(self):
        self.closed = True

    async def iter_daily_activity(self, start, end):
        if self.fail:
            raise RuntimeError("secret-response-must-never-escape")
        if False:
            yield


@pytest.mark.asyncio
async def test_sync_failures_are_not_reported_as_success(isolated, monkeypatch):
    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    adapter.fail = True
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    messages = []
    result = await backend.sync("2026-07-01", "2026-07-08", messages.append)
    assert result["status"] == "error" and not result["all_ok"]
    assert result["types"]["daily_activity"]["failed_chunks"] == 2
    assert "secret-response" not in str(result) + str(messages)
    assert adapter.closed


@pytest.mark.asyncio
async def test_cancel_closes_adapter_and_releases_lock(isolated, monkeypatch):
    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    adapter.block = True
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    task = asyncio.create_task(backend.sync("2026-07-01", "2026-07-08", lambda _: None))
    await adapter.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.closed
    assert not backend._operation_lock.locked()


@pytest.mark.asyncio
async def test_partial_type_preserves_bad_record_status(isolated, monkeypatch):
    from mi_fitness_mcp.services.sync_service import SyncService

    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})

    async def mixed_result(*args, **kwargs):
        return {
            "status": "ok", "added": 1,
            "chunks": [{"bad_records": [{"error": "secret-response"}]}],
        }

    monkeypatch.setattr(SyncService, "sync_data_type", mixed_result)
    result = await backend.sync("2026-07-01", "2026-07-01", lambda _: None)
    assert result["status"] == "partial" and not result["all_ok"]
    assert "secret-response" not in str(result)
    assert result["types"]["daily_activity"]["status"] == "partial"


@pytest.mark.asyncio
async def test_malformed_upstream_counts_cannot_leak_error_values(isolated, monkeypatch):
    from mi_fitness_mcp.services.sync_service import SyncService

    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})

    async def malformed_result(*args, **kwargs):
        return {"status": "ok", "added": "secret-response"}

    monkeypatch.setattr(SyncService, "sync_data_type", malformed_result)
    with pytest.raises(backend.DesktopError) as error:
        await backend.sync("2026-07-01", "2026-07-01", lambda _: None)
    assert "secret-response" not in str(error.value)
    assert adapter.closed
    assert not backend._operation_lock.locked()


@pytest.mark.asyncio
async def test_successful_chunk_and_failed_chunk_produce_partial(isolated, monkeypatch):
    from mi_fitness_mcp.services.sync_service import SyncService

    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    monkeypatch.setattr(backend, "DATA_TYPES", {"daily_activity": "日常活动"})
    calls = []

    async def chunk_result(self, name, *, start_date, end_date, force_full):
        calls.append((start_date, end_date, force_full))
        return {"status": "ok" if len(calls) == 1 else "error"}

    monkeypatch.setattr(SyncService, "sync_data_type", chunk_result)
    result = await backend.sync("2026-07-01", "2026-07-08", lambda _: None)
    assert result["partial"] and not result["all_ok"]
    assert calls == [
        ("2026-07-01", "2026-07-07", True), ("2026-07-08", "2026-07-08", True),
    ]


@pytest.mark.asyncio
async def test_changed_login_account_never_creates_database(isolated, monkeypatch):
    backend.save_credentials("123456", "fake-token")
    adapter = FakeAdapter()
    adapter.user_id = "678901"
    monkeypatch.setattr(backend, "_new_adapter", lambda *args: adapter)
    result = await backend.sync("2026-07-01", "2026-07-08", lambda _: None)
    assert result["status"] == "error"
    assert not list(backend.DATA_DIR.rglob("mi_fitness.db"))
    assert adapter.closed


def test_account_guard_rejects_foreign_records():
    from types import SimpleNamespace

    called = []
    guarded = backend._AccountDatabase(
        SimpleNamespace(insert_daily_activity=called.append), "123456"
    )
    with pytest.raises(backend.DesktopError):
        guarded.insert_daily_activity(SimpleNamespace(user_id="678901"))
    assert called == []


def test_smoke_demo_real_exports_include_both_day_boundaries(isolated):
    result = backend.smoke_demo()
    assert result["total_rows"] == 3 and not result["empty"]
    assert not backend.DATA_DIR.exists() and not isolated.values
    payload = json.loads(
        (backend.Path(result["directory"]) / "mi_fitness.json").read_text(encoding="utf-8")
    )
    assert len(payload["records"]["daily_activity"]) == 1
    heart_rows = payload["records"]["heart_rate"]
    assert len(heart_rows) == 2
    assert heart_rows[0]["timestamp"].endswith("00:00:00")
    assert heart_rows[1]["timestamp"].endswith("23:59:59")
    csv_path = backend.Path(result["directory"]) / "csv" / "heart_rate.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 2
    assert not list(backend.DEMO_DIR.rglob(".export-snapshot.db"))


def test_empty_export_is_explicit_and_export_never_reads_vault(isolated, monkeypatch):
    from mi_fitness_mcp.storage import Database

    backend.save_credentials("123456", "fake-token")
    settings = backend._settings()
    Database(backend.DATA_DIR / settings["account_key"] / "mi_fitness.db")
    monkeypatch.setattr(backend, "_vault", lambda: pytest.fail("export read credentials"))
    messages = []
    result = backend.export("2026-07-01", "2026-07-01", messages.append)
    again = backend.export("2026-07-01", "2026-07-01", messages.append)
    assert result["directory"] != again["directory"]
    assert result["empty"] and result["total_rows"] == 0
    assert any("没有本地记录" in message for message in messages)


def test_export_shanghai_offset_and_utc_boundaries_preserve_source(isolated):
    from mi_fitness_mcp.models import HeartRateSample
    from mi_fitness_mcp.storage import Database

    backend.save_credentials("123456", "fake-token")
    db_path = backend.DATA_DIR / backend._settings()["account_key"] / "mi_fitness.db"
    database = Database(db_path)
    timestamps = [
        "2026-07-14T23:59:59+08:00",
        "2026-07-15T00:00:00+08:00",
        "2026-07-15T23:59:59+08:00",
        "2026-07-16T00:00:00+08:00",
        "2026-07-14T15:59:59+00:00",
        "2026-07-14T16:00:00+00:00",
        "2026-07-15T15:59:59+00:00",
        "2026-07-15T16:00:00+00:00",
    ]
    for index, timestamp in enumerate(timestamps):
        database.insert_heart_rate_sample(HeartRateSample(
            id=f"offset-{index}", timestamp=datetime.fromisoformat(timestamp), bpm=70,
            provider="mi_fitness", source_type="cloud_session", user_id="123456",
        ))
    before = db_path.read_bytes()
    result = backend.export("2026-07-15", "2026-07-15", lambda _: None)
    payload = json.loads(
        (backend.Path(result["directory"]) / "mi_fitness.json").read_text(encoding="utf-8")
    )
    exported = {record["timestamp"] for record in payload["records"]["heart_rate"]}
    assert exported == {timestamps[index] for index in (1, 2, 5, 6)}
    assert result["total_rows"] == 4
    assert payload["filters"]["start_date"] == "2026-07-15"
    assert payload["filters"]["end_date"] == "2026-07-15"
    assert payload["filters"]["timezone"] == "Asia/Shanghai"
    assert db_path.read_bytes() == before


def test_invalid_cached_date_fails_safely_without_changing_source(isolated):
    from mi_fitness_mcp.storage import Database

    backend.save_credentials("123456", "fake-token")
    db_path = backend.DATA_DIR / backend._settings()["account_key"] / "mi_fitness.db"
    Database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO heart_rate_samples "
            "(id,provider,source_type,user_id,timestamp,bpm,sample_type) "
            "VALUES ('bad','mi_fitness','cloud_session','123456',?,70,'passive')",
            ("secret-invalid-timestamp",),
        )
    before = db_path.read_bytes()
    with pytest.raises(backend.DesktopError) as error:
        backend.export("2026-07-15", "2026-07-15", lambda _: None)
    assert "secret-invalid-timestamp" not in str(error.value)
    assert db_path.read_bytes() == before
    assert not list(backend.EXPORTS_DIR.rglob(".export-snapshot.db"))
