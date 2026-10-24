"""Start/stop the local backend without losing the page's loopback launcher."""

import asyncio
import threading
from unittest.mock import Mock

import local_server
from test_local_server import request, server as server


def test_stopped_backend_keeps_launcher_and_restarts_with_same_session(server, monkeypatch):
    initial_control, initial_databases = server.control, server.databases
    token = server.token
    server.databases.inventory = Mock(side_effect=AssertionError("stopped database access"))
    assert request(server, "/api/shutdown", {})[1] == {"status": "closed"}
    assert server.backend_stopped.wait(3)
    credentials = Mock(side_effect=AssertionError("stopped credential access"))
    monkeypatch.setattr(local_server.backend, "credentials_status", credentials)
    state = request(server, "/api/status")[1]
    assert state["closed"] and not state["stopping"] and state["account"] is None
    assert not server.closed.is_set() and server.thread.is_alive()
    assert request(server, "/")[0] == 200
    assert request(server, "/health")[0] == 200
    assert request(server, "/api/accounts")[0] == 409
    for path, payload in (
        ("/api/cache", {"start": "2026-01-01", "end": "2026-01-02"}),
        ("/api/login", {}), ("/api/selftest", {}), ("/api/open-exports", {}),
        ("/api/credentials", {"user_id": "123456", "pass_token": "SYNTHETIC"}),
        ("/api/delete", {"token": "synthetic"}),
        ("/api/deletion-preview", {"account_key": "synthetic"}),
    ):
        assert request(server, path, payload)[0] == 409
    initial_databases.inventory.assert_not_called()
    credentials.assert_not_called()

    assert request(server, "/api/start", {})[1] == {"status": "ok"}
    assert server.control is not initial_control and not server.control.closed
    assert server.databases is not initial_databases and server.token == token
    assert not server.backend_stopped.is_set()
    credentials.assert_not_called()
    local_server.backend.sync.assert_not_called()
    local_server.login.assert_not_called()
    current = server.control
    assert request(server, "/api/start", {})[0] == 200
    assert server.control is current  # Repeated clicks do not replace active jobs.


def test_stop_cancels_sync_and_waits_for_cleanup_before_start(server, monkeypatch):
    started, cleaning, release, cleaned = (threading.Event() for _ in range(4))

    async def sync(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaning.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            cleaned.set()

    monkeypatch.setattr(local_server.backend, "sync", sync)
    request(server, "/api/sync", {"start": "2026-01-01", "end": "2026-01-02"})
    assert started.wait(2)
    try:
        assert request(server, "/api/shutdown", {})[0] == 200
        assert cleaning.wait(2)
        state = request(server, "/api/status")[1]
        assert state["closed"] and state["stopping"]
        assert request(server, "/api/start", {})[0] == 409
        assert request(server, "/api/shutdown", {})[0] == 200
    finally:
        release.set()
    assert server.backend_stopped.wait(3) and cleaned.is_set()
    assert request(server, "/api/start", {})[0] == 200
    assert not server.control.closed and server.control.job is None


def test_stop_allows_export_publication_to_finish(server, monkeypatch):
    writing, release, written = (threading.Event() for _ in range(3))

    def export(*args, **kwargs):
        writing.set()
        release.wait(5)
        written.set()
        return {"directory": "synthetic-export", "empty": False}

    monkeypatch.setattr(local_server.backend, "export", export)
    request(server, "/api/export", {"start": "2026-01-01", "end": "2026-01-02"})
    assert writing.wait(2)
    stopped_control = server.control
    try:
        assert request(server, "/api/shutdown", {})[0] == 200
        assert request(server, "/api/start", {})[0] == 409
        assert not server.backend_stopped.is_set()
    finally:
        release.set()
    assert server.backend_stopped.wait(3) and written.is_set()
    assert stopped_control.job["status"] == "ok"
    server.close()
    assert server.closed.is_set() and not server.thread.is_alive()


def test_start_endpoint_requires_the_existing_local_session(server):
    request(server, "/api/shutdown", {})
    assert server.backend_stopped.wait(3)
    assert request(server, "/api/start", {}, headers={"X-MiFit-Token": ""})[0] == 403
    assert request(server, "/api/start", {}, headers={"Origin": "https://example.invalid"})[0] == 403
    assert server.control.closed
