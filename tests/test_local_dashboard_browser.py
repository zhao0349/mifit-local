"""Browser-to-loopback integration with synthetic data and blocked cloud access."""

import asyncio

import pytest
from playwright.sync_api import expect

import local_server
from test_dashboard_browser import browser as browser
from test_dashboard_browser import raw_import_file as raw_import_file
from test_dashboard_browser import tiny_summary as tiny_summary
from test_local_server import server as server


@pytest.fixture
def live_page(browser, server, monkeypatch, tiny_summary):
    monkeypatch.setattr(server.control, "summary", lambda dates, **kwargs: tiny_summary)
    context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN")
    errors, external = [], []

    def route(item):
        if item.request.url.startswith((server.origin + "/", "file:", "blob:", "data:")):
            item.continue_()
        else:
            external.append(item.request.url)
            item.abort()

    context.route("**/*", route)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(server.url)
    expect(page.locator("#local-load")).to_be_enabled()
    page.locator("#local-start").fill("2026-01-01")
    page.locator("#local-end").fill("2026-01-03")
    try:
        yield page
    finally:
        context.close()
    assert not external
    assert not errors


def test_cache_load_and_explicit_sync_preserve_manual_import(live_page, monkeypatch, raw_import_file):
    page = live_page
    assert page.evaluate("MiFitDashboard.getData().days.length") == 0
    page.locator("#local-load").click()
    expect(page.locator("#archive-count")).to_contain_text("3 天")
    local_server.backend.sync.assert_not_called()
    page.locator("#import-file").set_input_files(str(raw_import_file))
    expect(page.locator("#toast")).to_contain_text("导入完成")
    manual = page.evaluate("JSON.stringify(MiFitDashboard.getData())")

    async def partial(*args):
        args[-1]("synthetic partial sync")
        return {"status": "partial", "types": {"sleep": {
            "status": "error", "added": 0, "updated": 0, "failed_chunks": 1,
        }}}

    monkeypatch.setattr(local_server.backend, "sync", partial)
    page.locator("#local-sync").click()
    expect(page.locator("#local-progress")).to_contain_text("部分完成")
    expect(page.locator("#local-progress")).to_contain_text("失败分块 1")
    assert page.evaluate("JSON.stringify(MiFitDashboard.getData())") == manual
    expect(page.locator("#import-button")).to_be_enabled()


def test_web_cancellation_and_login_flow(live_page, monkeypatch):
    page = live_page

    async def blocked(*args):
        await asyncio.sleep(60)

    monkeypatch.setattr(local_server.backend, "sync", blocked)
    page.locator("#local-sync").click()
    expect(page.locator("#local-cancel")).to_be_enabled()
    expect(page.locator("#local-login")).to_be_disabled()
    page.locator("#local-cancel").click()
    expect(page.locator("#local-progress")).to_contain_text("已取消")
    expect(page.locator("#local-login")).to_be_enabled()

    async def login(emit):
        emit("synthetic login")
        return "123456", "SYNTHETIC_PRIVATE_TOKEN"

    saved = []
    monkeypatch.setattr(local_server, "login", login)
    monkeypatch.setattr(local_server.backend, "save_credentials", lambda *args, **kwargs: saved.append(args))
    page.locator("#local-login").click()
    expect(page.locator("#local-progress")).to_contain_text("synthetic login")
    assert saved == [("123456", "SYNTHETIC_PRIVATE_TOKEN")]
    assert "SYNTHETIC_PRIVATE_TOKEN" not in page.content()


def test_saved_copy_removes_control_and_token_and_reopens_fully_offline(live_page, server, browser, tmp_path):
    page = live_page
    page.locator("#local-load").click()
    expect(page.locator("#archive-count")).to_contain_text("3 天")
    original = page.evaluate("JSON.stringify(MiFitDashboard.getData())")
    with page.expect_download() as pending:
        page.locator("#download-html").click()
    path = tmp_path / "synthetic-offline.html"
    pending.value.save_as(path)
    html = path.read_text(encoding="utf-8")
    assert server.token not in html
    assert 'id="local-panel"' not in html
    assert 'connect-src \'none\'' in html
    context = browser.new_context(offline=True)
    try:
        offline = context.new_page()
        requests = []
        offline.on("request", lambda request: requests.append(request.url))
        offline.goto(path.as_uri())
        expect(offline.locator("#backend-help")).to_be_visible()
        expect(offline.locator("#import-button")).to_be_enabled()
        assert offline.evaluate("JSON.stringify(MiFitDashboard.getData())") == original
        assert not any(url.startswith(("http:", "https:")) for url in requests)
    finally:
        context.close()


def test_cache_failure_and_disconnect_preserve_current_archive(live_page, server, monkeypatch):
    page = live_page
    page.locator("#local-load").click()
    expect(page.locator("#archive-count")).to_contain_text("3 天")
    original = page.evaluate("JSON.stringify(MiFitDashboard.getData())")

    def fail(dates, **kwargs):
        raise local_server.DashboardDataError("synthetic cache failure")

    monkeypatch.setattr(server.control, "summary", fail)
    page.locator("#local-load").click()
    expect(page.locator("#toast")).to_contain_text("synthetic cache failure")
    assert page.evaluate("JSON.stringify(MiFitDashboard.getData())") == original
    page.route("**/api/status", lambda route: route.abort())
    page.locator("#local-reconnect").click()
    expect(page.locator("#local-status")).to_contain_text("已断开")
    expect(page.locator("#local-load")).to_be_disabled()
    expect(page.locator("#import-button")).to_be_enabled()
    page.unroute("**/api/status")
    page.locator("#local-reconnect").click()
    expect(page.locator("#local-load")).to_be_enabled()
    assert page.evaluate("JSON.stringify(MiFitDashboard.getData())") == original


def test_manual_import_wins_over_cache_response_in_flight(live_page, raw_import_file):
    page = live_page
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (args[0] === '/api/cache') await new Promise(resolve => { window.releaseCache = resolve; document.documentElement.dataset.cacheReady = 'true'; });
        return response;
      };
    }""")
    page.locator("#local-load").click()
    expect(page.locator("html")).to_have_attribute("data-cache-ready", "true")
    page.locator("#import-file").set_input_files(str(raw_import_file))
    expect(page.locator("#toast")).to_contain_text("导入完成")
    imported = page.evaluate("JSON.stringify(MiFitDashboard.getData())")
    page.evaluate("releaseCache()")
    expect(page.locator("#toast")).to_contain_text("已保留手动导入")
    assert page.evaluate("JSON.stringify(MiFitDashboard.getData())") == imported


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_local_controls_fit_viewport(live_page, width):
    live_page.set_viewport_size({"width": width, "height": 900})
    assert live_page.evaluate("document.documentElement.scrollWidth") <= width
    expect(live_page.locator("#local-load")).to_be_visible()


def test_replacement_is_opt_in_confirmed_and_bound_to_account(live_page, monkeypatch):
    page = live_page
    calls = []
    async def sync(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "ok", "all_ok": True}
    monkeypatch.setattr(local_server.backend, "sync", sync)
    expect(page.locator("#local-replace")).not_to_be_checked()
    page.locator("#local-replace").check()
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#local-sync").click()
    expect(page.locator("#local-sync")).to_be_enabled()
    assert calls == []
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#local-sync").click()
    expect(page.locator("#local-progress")).to_contain_text("已完成")
    assert calls == [{"mode": "replace", "expected_account": "synthetic-account"}]
    expect(page.locator("#local-replace")).not_to_be_checked()


def test_cache_request_uses_all_history_by_default_and_explicit_range(live_page, server, monkeypatch, tiny_summary):
    calls = []
    def summary(dates, **kwargs):
        calls.append(kwargs)
        return tiny_summary
    monkeypatch.setattr(server.control, "summary", summary)
    live_page.locator("#local-load").click()
    expect(live_page.locator("#local-source")).to_contain_text("全部已缓存历史")
    live_page.locator("#local-read-scope").select_option("range")
    live_page.locator("#local-load").click()
    expect(live_page.locator("#local-source")).to_contain_text("2026-01-01 至 2026-01-03")
    assert calls == [{"scope": "all", "expected_account": "synthetic-account"},
                     {"scope": "range", "expected_account": "synthetic-account"}]
