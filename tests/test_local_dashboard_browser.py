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
    monkeypatch.setattr(server.databases, "inventory", lambda: {"accounts": [], "total_bytes": 0})
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
    expect(page.locator("#local-panel")).not_to_be_visible()
    page.locator("#backend-toggle").click()
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
    page.locator("#local-close").click()
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
        expect(offline.locator("#backend-label")).to_have_text("后端未连接")
        offline.locator("#backend-toggle").click()
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
    page.locator("#local-sync").click()
    expect(page.locator("#local-confirm")).to_be_visible()
    page.locator("#local-confirm-cancel").click()
    expect(page.locator("#local-sync")).to_be_enabled()
    assert calls == []
    page.locator("#local-sync").click()
    page.locator("#local-confirm-accept").click()
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


def synthetic_inventory():
    labels = [("daily_activity", "日常活动"), ("heart_rate", "心率"), ("sleep", "睡眠"),
              ("body_measurements", "体重"), ("spo2", "血氧"), ("stress", "压力"),
              ("workouts", "运动记录")]
    categories = [{"key": key, "label": label, "count": 2, "start": "2026-01-01", "end": "2026-01-03"}
                  for key, label in labels]
    return {"accounts": [{"account_key": "synthetic-account", "label": "合成账号 00", "current": True,
                          "database_bytes": 40960, "db_bytes": 32768, "wal_bytes": 8192, "shm_bytes": 0,
                          "total_rows": 14, "start": "2026-01-01", "end": "2026-01-03", "categories": categories}],
            "total_bytes": 40960, "categories": categories}


def open_database(page, server, monkeypatch):
    inventory = synthetic_inventory()
    monkeypatch.setattr(server.databases, "inventory", lambda: inventory)
    page.locator("#local-database > summary").click()
    expect(page.locator("#db-total")).to_contain_text("1 个账号数据库")
    expect(page.locator("#db-account-details")).not_to_be_visible()
    page.locator("#db-account").select_option("synthetic-account")
    return inventory


def test_database_filters_preview_and_confirmation(live_page, server, monkeypatch):
    page = live_page
    inventory = open_database(page, server, monkeypatch)
    expect(page.locator("#db-account-summary")).to_contain_text("40.0 KiB")
    expect(page.locator("#db-category-summary tr")).to_have_count(7)
    expect(page.locator("#db-categories input")).to_have_count(7)
    previews, deleted = [], []

    def preview(**payload):
        previews.append(payload)
        return {"token": "synthetic-preview", "account_key": payload["account_key"],
                "account_label": "合成账号 00", "all_data": False, "wait_seconds": 0,
                "start": payload["start"], "end": payload["end"], "total_rows": 2,
                "categories": [inventory["categories"][3]]}

    monkeypatch.setattr(server.databases, "preview", preview)
    monkeypatch.setattr(server.databases, "delete", lambda token: deleted.append(token) or {"status": "ok"})
    page.locator("#db-start").fill("2026-01-02")
    page.locator("#db-end").fill("2026-01-03")
    page.locator('#db-categories input[value="body_measurements"]').check()
    page.locator("#db-delete").click()
    expect(page.locator("#local-confirm-message")).to_contain_text("合成账号 00")
    expect(page.locator("#local-confirm-message")).to_contain_text("体重")
    expect(page.locator("#local-confirm-message")).to_contain_text("2026-01-02 至 2026-01-03")
    assert deleted == []
    assert page.locator("#local-confirm-accept").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(229, 28, 42)"
    page.locator("#local-confirm-cancel").click()
    assert deleted == []
    page.locator("#db-delete").click()
    page.locator("#local-confirm-accept").click()
    expect(page.locator("#local-feedback")).to_contain_text("已删除")
    assert deleted == ["synthetic-preview"]
    assert previews == [{"account_key": "synthetic-account", "start": "2026-01-02", "end": "2026-01-03",
                         "categories": ["body_measurements"]}] * 2


@pytest.mark.parametrize("button", ["db-delete", "db-delete-account"])
def test_whole_account_deletion_requires_three_second_confirmation(live_page, server, monkeypatch, button):
    page = live_page
    inventory = open_database(page, server, monkeypatch)
    deleted, previews = [], []

    def preview(**payload):
        previews.append(payload)
        return {"token": "synthetic-all", "account_key": payload["account_key"],
                "account_label": "合成账号 00", "all_data": True, "wait_seconds": 3,
                "start": None, "end": None, "total_rows": 14, "categories": inventory["categories"]}

    monkeypatch.setattr(server.databases, "preview", preview)
    monkeypatch.setattr(server.databases, "delete", lambda token: deleted.append(token) or {"status": "ok"})
    page.locator("#" + button).click()
    expect(page.locator("#local-confirm-accept")).to_be_disabled()
    expect(page.locator("#local-confirm-title")).to_contain_text("全部数据")
    assert deleted == []
    expect(page.locator("#local-confirm-accept")).to_be_enabled(timeout=6000)
    page.locator("#local-confirm-accept").click()
    expect(page.locator("#local-feedback")).to_contain_text("已删除")
    assert deleted == ["synthetic-all"]
    assert previews == [{"account_key": "synthetic-account", "start": None, "end": None, "categories": []}]


def test_manual_credentials_and_export_controls(live_page, server, monkeypatch):
    page = live_page
    saved, exports = [], []
    monkeypatch.setattr(local_server.backend, "save_credentials", lambda *args, **kwargs: saved.append(args) or {})
    page.locator("#local-manual summary").click()
    page.locator("#local-user-id").fill("123456")
    page.locator("#local-pass-token").fill("SYNTHETIC_PRIVATE_TOKEN")
    page.locator("#local-save-credentials").click()
    expect(page.locator("#local-feedback")).to_contain_text("凭据已保存")
    expect(page.locator("#local-user-id")).to_have_value("")
    expect(page.locator("#local-pass-token")).to_have_value("")
    assert saved == [("123456", "SYNTHETIC_PRIVATE_TOKEN")]
    assert "SYNTHETIC_PRIVATE_TOKEN" not in page.content()

    def export(start, end, emit, **kwargs):
        exports.append((start, end, kwargs))
        emit("synthetic export complete")
        return {"directory": "synthetic/export", "empty": False}

    monkeypatch.setattr(local_server.backend, "export", export)
    page.locator("#local-export").click()
    expect(page.locator("#local-progress")).to_contain_text("synthetic export complete")
    expect(page.locator("#local-export")).to_be_enabled()
    page.locator("#local-snapshot").click()
    expect(page.locator("#local-snapshot")).to_be_enabled()
    assert exports == [("2026-01-01", "2026-01-03", {"mode": "cumulative"}),
                       ("2026-01-01", "2026-01-03", {"mode": "snapshot"})]


def test_status_indicator_tracks_sync_disconnect_and_explicit_shutdown(live_page, server, monkeypatch):
    page = live_page
    expect(page.locator("#backend-label")).to_have_text("后端已连接")

    async def blocked(*args):
        await asyncio.sleep(60)

    monkeypatch.setattr(local_server.backend, "sync", blocked)
    page.locator("#local-sync").click()
    expect(page.locator("#backend-label")).to_have_text("后端正在同步")
    page.locator("#local-cancel").click()
    expect(page.locator("#backend-label")).to_have_text("后端已连接")
    page.route("**/api/status", lambda route: route.abort())
    page.locator("#local-reconnect").click()
    expect(page.locator("#backend-label")).to_have_text("后端未连接")
    page.unroute("**/api/status")
    page.locator("#local-reconnect").click()
    expect(page.locator("#backend-label")).to_have_text("后端已连接")
    monkeypatch.setattr(server, "request_shutdown", lambda: None)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#local-shutdown").click()
    expect(page.locator("#backend-label")).to_have_text("后端已关闭")
    expect(page.locator("#local-load")).to_be_disabled()
    expect(page.locator("#import-button")).to_be_enabled()
