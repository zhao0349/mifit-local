"""Headless launcher lifecycle; no real browser or account access."""

import threading
from unittest.mock import Mock

import pytest

import desktop_app


def test_launcher_opens_page_and_waits_for_full_launcher_shutdown():
    server = Mock(url="http://127.0.0.1:12345/")
    server.start.return_value = server
    server.closed = threading.Event()
    opened = Mock(side_effect=lambda url: server.closed.set() or True)
    desktop_app.run_service(server_factory=lambda: server, open_browser=opened)
    opened.assert_called_once_with(server.url)
    server.close.assert_called_once()


def test_failed_browser_start_closes_service():
    server = Mock()
    server.start.return_value = server
    with pytest.raises(RuntimeError):
        desktop_app.run_service(server_factory=lambda: server, open_browser=lambda url: False)
    server.close.assert_called_once()


@pytest.mark.parametrize("matches", [True, False])
def test_duplicate_launch_reopens_only_matching_service(monkeypatch, matches):
    response = Mock(status=200)
    identity = {"service": "mifit-local", "version": 2, "installation": "expected" if matches else "another"}
    response.read.return_value = desktop_app.json.dumps(identity).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(desktop_app.http.client, "HTTPConnection", lambda *a, **k: connection)
    monkeypatch.setattr(desktop_app, "installation_id", lambda: "expected")
    opened = Mock(return_value=True)
    monkeypatch.setattr(desktop_app.webbrowser, "open", opened)
    assert desktop_app.reopen_running() is matches
    assert opened.called is matches
    connection.close.assert_called_once()
