"""Synthetic loopback integration; never read real accounts or contact Xiaomi."""

import asyncio
import http.client
import json
import sqlite3
import threading
import time
from unittest.mock import Mock

import pytest

import dashboard_data
import local_server


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(local_server.backend, "interface_lock", threading.Lock())
    monkeypatch.setattr(local_server.backend, "account_scope", lambda: "synthetic-account")
    monkeypatch.setattr(local_server.backend, "credentials_status", lambda: {
        "configured": True, "keyring_available": True, "account_masked": "******00",
    })
    for name in ("sync", "save_credentials", "export", "smoke_demo"):
        monkeypatch.setattr(local_server.backend, name,
                            Mock(side_effect=AssertionError("real backend forbidden")))
    monkeypatch.setattr(local_server, "login", Mock(side_effect=AssertionError("real login forbidden")))
    monkeypatch.setattr(local_server, "resolve_current_database",
                        Mock(side_effect=AssertionError("real cache forbidden")))
    instance = local_server.LocalServer(port=0).start()
    try:
        yield instance
    finally:
        instance.close()
    assert not local_server.backend.interface_lock.locked()


def request(server, path, payload=None, *, headers=None, method=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_port, timeout=5)
    try:
        supplied = {"X-MiFit-Token": server.token, "Content-Type": "application/json"}
        supplied.update(headers or {})
        body = json.dumps(payload) if payload is not None else None
        connection.request(method or ("GET" if body is None else "POST"), path, body, supplied)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, value, dict(response.getheaders())
    finally:
        connection.close()


def wait_job(server):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = request(server, "/api/status")[1]
        if state["job"]["status"] != "running":
            return state["job"]
        time.sleep(0.01)
    pytest.fail("synthetic job did not finish")


def test_initial_page_and_status_never_sync_or_read_cache(server):
    code, html, headers = request(server, "/")
    assert code == 200
    assert "connect-src 'self'" in html and server.token in html
    assert "local-panel" in html and "import-file" in html
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    code, state, _ = request(server, "/api/status")
    assert code == 200 and not state["busy"] and state["job"] is None
    local_server.backend.sync.assert_not_called()
    local_server.resolve_current_database.assert_not_called()
    local_server.login.assert_not_called()


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Host": "localhost:17865"},
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"}, {"X-MiFit-Token": ""},
    {"X-MiFit-Token": "wrong"},
])
def test_api_rejects_cross_origin_rebinding_and_missing_session(server, headers):
    for path, body in (("/api/status", None), ("/api/login", {})):
        code, _, response_headers = request(server, path, body, headers=headers)
        assert code == 403
        assert "Access-Control-Allow-Origin" not in response_headers
    local_server.login.assert_not_called()


def test_no_file_serving_or_cors(server):
    for path in ("/data/settings.json", "/../desktop_backend.py", "/?token=bad"):
        assert request(server, path)[0] == 404
    assert request(server, "/api/sync", method="OPTIONS")[0] == 403


def test_rejected_post_body_is_drained_before_connection_closes(server):
    for _ in range(20):
        code, payload, _ = request(server, "/api/login", {}, headers={"X-MiFit-Token": "wrong"})
        assert code == 403 and payload["error"]
    local_server.login.assert_not_called()


def test_sync_returns_job_progress_partial_results_and_cannot_overlap(server, monkeypatch):
    release = threading.Event()

    async def sync(start, end, emit):
        assert (start, end) == ("2026-01-01", "2026-01-02")
        emit("synthetic progress")
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"status": "partial", "all_ok": False, "types": {"sleep": {"status": "error"}}}

    monkeypatch.setattr(local_server.backend, "sync", sync)
    code, job, _ = request(server, "/api/sync", {"start": "2026-01-01", "end": "2026-01-02"})
    assert code == 202 and job["status"] == "running"
    try:
        assert request(server, "/api/login", {})[0] == 409
        assert request(server, "/api/cache", {"start": "2026-01-01", "end": "2026-01-02"})[0] == 409
    finally:
        release.set()
    finished = wait_job(server)
    assert finished["id"] == job["id"] and finished["status"] == "partial"
    assert "synthetic progress" in finished["messages"]
    assert not request(server, "/api/status")[1]["busy"]


def test_cancel_waits_for_cleanup_and_repeated_cancel_is_safe(server, monkeypatch):
    started, cleaning, cleaned = threading.Event(), threading.Event(), threading.Event()

    async def sync(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaning.set()
            await asyncio.sleep(0.1)
            cleaned.set()

    monkeypatch.setattr(local_server.backend, "sync", sync)
    job = request(server, "/api/sync", {"start": "2026-01-01", "end": "2026-01-01"})[1]
    assert started.wait(2)
    assert request(server, "/api/cancel", {"id": "stale"})[0] == 400
    assert request(server, "/api/cancel", {"id": job["id"]})[0] == 200
    assert cleaning.wait(2)
    assert request(server, "/api/cancel", {"id": job["id"]})[0] == 200
    assert wait_job(server)["status"] == "cancelled"
    assert cleaned.is_set()


def test_login_stays_local_and_unexpected_errors_are_redacted(server, monkeypatch):
    async def login(emit):
        return "123456", "SYNTHETIC_SECRET"

    saved = Mock()
    monkeypatch.setattr(local_server, "login", login)
    monkeypatch.setattr(local_server.backend, "save_credentials", saved)
    assert request(server, "/api/login", {})[0] == 202
    job = wait_job(server)
    assert job["status"] == "ok"
    saved.assert_called_once_with("123456", "SYNTHETIC_SECRET", region="cn")
    assert "SYNTHETIC_SECRET" not in json.dumps(job) and "123456" not in json.dumps(job)
    saved.side_effect = RuntimeError("SYNTHETIC_SECRET")
    request(server, "/api/login", {})
    job = wait_job(server)
    assert job["status"] == "error" and "SYNTHETIC_SECRET" not in json.dumps(job)


@pytest.mark.parametrize("payload", [
    {}, [], {"start": "2026-01-01", "end": "2025-01-01"},
    {"start": "2026-02-30", "end": "2026-03-01"},
    {"start": "2026-01-01", "end": "2026-01-02", "path": "private.db"},
])
def test_invalid_ranges_never_start_sync(server, payload):
    assert request(server, "/api/sync", payload)[0] == 400
    local_server.backend.sync.assert_not_called()


def test_date_filtered_cache_is_read_only_and_includes_shanghai_boundaries(server, monkeypatch, tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as db:
        db.executescript(dashboard_data._DEMO_SCHEMA)
        db.execute("INSERT INTO daily_activity(date,steps) VALUES ('2026-01-01',1234)")
        for stamp in ("2025-12-31T15:59:59Z", "2025-12-31T16:00:00Z", "2026-01-01T15:59:59Z", "2026-01-01T16:00:00Z"):
            db.execute("INSERT INTO heart_rate_samples VALUES (?,70,'passive')", (stamp,))
    before = path.read_bytes()
    monkeypatch.setattr(local_server, "resolve_current_database", lambda: path)
    code, summary, _ = request(server, "/api/cache", {"start": "2026-01-01", "end": "2026-01-01", "scope": "range"})
    assert code == 200
    assert [row["date"] for row in summary["days"]] == ["2026-01-01"]
    assert summary["days"][0]["steps"] == 1234
    rows = summary["record_details"]["datasets"]["heart_rate"]["rows"]
    assert len(rows) == 2
    assert summary["meta"]["source_counts"]["heart_rate"] == 2
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_desktop_lock_blocks_web_actions(server):
    with local_server.backend.interface_lock:
        assert request(server, "/api/login", {})[0] == 409
        assert request(server, "/api/status")[1]["busy"]


def test_server_close_cancels_owned_job(server, monkeypatch):
    cleaned = threading.Event()
    started = threading.Event()

    async def sync(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.set()

    monkeypatch.setattr(local_server.backend, "sync", sync)
    request(server, "/api/sync", {"start": "2026-01-01", "end": "2026-01-01"})
    assert started.wait(2)
    server.control.close()
    assert cleaned.is_set()
    assert not server.control.worker.is_alive()


def test_replacement_requires_current_account_confirmation(server):
    for account in (None, "stale-account"):
        payload = {"start": "2026-01-01", "end": "2026-01-02", "mode": "replace", "account_scope": account}
        assert request(server, "/api/sync", payload)[0] == 409
        assert not local_server.backend.interface_lock.locked()
    local_server.backend.sync.assert_not_called()


def test_cache_defaults_to_whole_history_and_rejects_stale_account(server, monkeypatch):
    called = []
    monkeypatch.setattr(local_server, "resolve_current_database", lambda: "synthetic.db")
    monkeypatch.setattr(local_server, "build_summary", lambda path, **kwargs: called.append((path, kwargs)) or {})
    dates = {"start": "2026-01-01", "end": "2026-01-02"}
    assert request(server, "/api/cache", dates)[0] == 200
    assert called == [("synthetic.db", {})]
    assert request(server, "/api/cache", {**dates, "account_scope": "old-account"})[0] == 409
    assert len(called) == 1


def test_manual_credentials_are_saved_without_echo(server, monkeypatch):
    saved = Mock(return_value={"configured": True, "account_masked": "******00"})
    monkeypatch.setattr(local_server.backend, "save_credentials", saved)
    payload = {"user_id": "12345600", "pass_token": "SYNTHETIC_TOKEN" * 300}
    code, body, _ = request(server, "/api/credentials", payload)
    assert code == 200 and body["configured"]
    saved.assert_called_once_with(payload["user_id"], payload["pass_token"], region="cn")
    assert "SYNTHETIC_TOKEN" not in json.dumps(body)
    assert "12345600" not in json.dumps(request(server, "/api/status")[1])


def test_export_and_snapshot_jobs_and_selftest(server, monkeypatch):
    exported = Mock(return_value={"directory": "synthetic", "empty": False})
    monkeypatch.setattr(local_server.backend, "export", exported)
    for mode in ("cumulative", "snapshot"):
        code, job, _ = request(server, "/api/export", {
            "start": "2026-01-01", "end": "2026-01-02", "mode": mode,
            "account_scope": "synthetic-account",
        })
        assert code == 202 and not job["cancellable"]
        assert wait_job(server)["status"] == "ok"
        assert exported.call_args.kwargs == {"mode": mode}
    monkeypatch.setattr(local_server.backend, "smoke_demo", lambda: {"directory": "synthetic-demo"})
    assert request(server, "/api/selftest", {})[0] == 202
    assert wait_job(server)["status"] == "ok"


def test_sync_export_waits_for_publication_and_cancelled_sync_does_not_export(server, monkeypatch):
    writing, release = threading.Event(), threading.Event()

    async def sync(*args):
        return {"status": "ok", "all_ok": True}

    def export(*args):
        writing.set()
        release.wait(3)
        return {"directory": "synthetic"}

    monkeypatch.setattr(local_server.backend, "sync", sync)
    monkeypatch.setattr(local_server.backend, "export", export)
    payload = {"start": "2026-01-01", "end": "2026-01-02", "export": True}
    job = request(server, "/api/sync", payload)[1]
    try:
        assert writing.wait(2)
        assert not request(server, "/api/status")[1]["job"]["cancellable"]
        request(server, "/api/cancel", {"id": job["id"]})
    finally:
        release.set()
    assert wait_job(server)["status"] == "ok"

    async def blocked(*args):
        await asyncio.sleep(60)

    exported = Mock()
    monkeypatch.setattr(local_server.backend, "sync", blocked)
    monkeypatch.setattr(local_server.backend, "export", exported)
    job = request(server, "/api/sync", payload)[1]
    request(server, "/api/cancel", {"id": job["id"]})
    assert wait_job(server)["status"] == "cancelled"
    exported.assert_not_called()


def test_database_routes_require_one_account_and_confirmed_token(server):
    server.databases = Mock()
    server.databases.inventory.return_value = {"accounts": [], "categories": []}
    server.databases.preview.return_value = {"token": "confirmation", "all_data": True, "wait_seconds": 3}
    server.databases.delete.return_value = {"status": "ok"}
    assert request(server, "/api/accounts")[0] == 200
    assert request(server, "/api/deletion-preview", {})[0] == 400
    assert request(server, "/api/deletion-preview", {"accounts": ["one", "two"]})[0] == 400
    assert request(server, "/api/deletion-preview", {"account_key": "one", "categories": []})[0] == 200
    server.databases.preview.assert_called_once_with(account_key="one", categories=[])
    assert request(server, "/api/delete", {"account_key": "one"})[0] == 404
    server.databases.delete.assert_not_called()
    assert request(server, "/api/delete", {"token": "confirmation"})[0] == 200
    server.databases.delete.assert_called_once_with("confirmation")


def test_shutdown_stops_server_and_cancels_sync(server, monkeypatch):
    cleaned = threading.Event()

    async def sync(*args):
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.set()

    monkeypatch.setattr(local_server.backend, "sync", sync)
    request(server, "/api/sync", {"start": "2026-01-01", "end": "2026-01-02"})
    assert request(server, "/api/shutdown", {})[1] == {"status": "closed"}
    assert server.closed.wait(3)
    assert cleaned.is_set()
    assert not server.thread.is_alive()
