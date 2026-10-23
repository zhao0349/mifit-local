"""Synthetic cookie and mocked browser tests; no Xiaomi network requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import official_login
from official_login import OfficialLoginError, select_credentials

NOW = 2_000_000_000
USER = "synthetic-user"
TOKEN = "synthetic-token"


def cookie(name, value, **overrides):
    result = {
        "name": name,
        "value": value,
        "domain": ".xiaomi.com",
        "path": "/",
        "secure": True,
        "expires": -1,
    }
    result.update(overrides)
    return result


def pair(**overrides):
    return [
        cookie("userId", USER, **overrides),
        cookie("passToken", TOKEN, **overrides),
    ]


@pytest.mark.parametrize(
    "domain", [".xiaomi.com", ".account.xiaomi.com", "account.xiaomi.com"]
)
def test_accepts_official_domain_pair(domain):
    assert select_credentials(pair(domain=domain), now=NOW) == (USER, TOKEN)


@pytest.mark.parametrize(
    "domain",
    [
        "account.xiaomi.com.evil.example",
        ".evilxiaomi.com",
        "not-account.xiaomi.com",
        ".com",
        "xiaomi.com",  # A host-only parent cookie does not apply to account.
        "",
    ],
)
def test_rejects_other_domains(domain):
    assert select_credentials(pair(domain=domain), now=NOW) is None


@pytest.mark.parametrize("missing", ["userId", "passToken"])
def test_requires_both_cookies(missing):
    assert select_credentials(
        [item for item in pair() if item["name"] != missing], now=NOW
    ) is None


@pytest.mark.parametrize(
    "expiry", [0, NOW - 1, NOW, float("nan"), float("inf"), "future", None, True, -2]
)
def test_rejects_expired_or_malformed_expiry(expiry):
    assert select_credentials(pair(expires=expiry), now=NOW) is None


def test_accepts_future_expiry():
    assert select_credentials(pair(expires=NOW + 10), now=NOW) == (USER, TOKEN)


@pytest.mark.parametrize(
    "value", ["", "two words", "x;y", "x\r\ny", "x\ty", "x\x00y", "x\x7fy",
              'x"y', "x,y", "x\\y", "中文", "x\u00a0y", None, 123]
)
def test_rejects_invalid_cookie_values(value):
    cookies = [cookie("userId", USER), cookie("passToken", value)]
    assert select_credentials(cookies, now=NOW) is None


@pytest.mark.parametrize("secure", [True, False])
@pytest.mark.parametrize("path", ["/", "/pass", "/pass/"])
def test_accepts_cookies_applicable_to_official_https_urls(secure, path):
    assert select_credentials(pair(secure=secure, path=path), now=NOW) == (USER, TOKEN)


@pytest.mark.parametrize("path", ["/unrelated", "/p", "/passive", "/pass/deeper", "", None])
def test_rejects_paths_not_applicable_to_requested_urls(path):
    assert select_credentials(pair(path=path), now=NOW) is None


def test_accepts_unique_names_from_different_official_cookie_scopes():
    cookies = [
        cookie("userId", USER, domain=".xiaomi.com", secure=False),
        cookie("passToken", TOKEN, domain=".account.xiaomi.com", path="/pass/"),
    ]
    assert select_credentials(cookies, now=NOW) == (USER, TOKEN)


def test_deduplicates_identical_values_across_official_scopes():
    cookies = pair() + pair(domain=".account.xiaomi.com", path="/pass/")
    assert select_credentials(cookies, now=NOW) == (USER, TOKEN)


def test_rejects_conflicting_complete_sessions():
    cookies = pair(domain="account.xiaomi.com") + [
        cookie("userId", "another-synthetic-user"),
        cookie("passToken", "another-synthetic-token"),
    ]
    assert select_credentials(cookies, now=NOW) is None


def test_rejects_ambiguous_same_domain_value():
    cookies = pair() + [cookie("passToken", "another-synthetic-token")]
    assert select_credentials(cookies, now=NOW) is None


@pytest.mark.parametrize("name", ["userId", "passToken"])
def test_rejects_conflict_even_if_other_domain_has_only_one_cookie(name):
    cookies = pair() + [
        cookie(name, "conflicting-synthetic-value", domain="account.xiaomi.com")
    ]
    assert select_credentials(cookies, now=NOW) is None


def test_other_domain_cannot_complete_the_official_pair():
    cookies = [
        cookie("userId", USER),
        cookie("passToken", TOKEN, domain="account.xiaomi.com.evil.example"),
    ]
    assert select_credentials(cookies, now=NOW) is None


def browser_fixture(monkeypatch, cookies=None):
    page = SimpleNamespace(
        goto=AsyncMock(),
        is_closed=Mock(return_value=False),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        cookies=AsyncMock(return_value=pair() if cookies is None else cookies),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        is_connected=Mock(return_value=True),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
    monkeypatch.setattr(official_login, "_playwright_factory", lambda: manager)
    return page, context, browser, playwright


def assert_closed(context, browser, playwright):
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_uses_isolated_visible_edge_and_never_emits_secrets(monkeypatch):
    page, context, browser, playwright = browser_fixture(monkeypatch)
    messages = []
    result = await official_login.login(messages.append)
    assert result == (USER, TOKEN)
    playwright.chromium.launch.assert_awaited_once_with(
        channel="msedge", headless=False, chromium_sandbox=True, timeout=30000
    )
    browser.new_context.assert_awaited_once_with(accept_downloads=False)
    page.goto.assert_awaited_once_with(
        official_login.LOGIN_URL, wait_until="domcontentloaded", timeout=60000
    )
    context.cookies.assert_awaited_once_with(list(official_login.COOKIE_URLS))
    assert all(USER not in message and TOKEN not in message for message in messages)
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_user_closing_browser_has_fixed_message_and_cleanup(monkeypatch):
    _, context, browser, playwright = browser_fixture(monkeypatch)
    browser.is_connected.return_value = False
    with pytest.raises(OfficialLoginError, match=official_login.CLOSED_ERROR):
        await official_login.login(lambda _: None)
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_start_failure_hides_underlying_exception(monkeypatch):
    _, _, _, playwright = browser_fixture(monkeypatch)
    playwright.chromium.launch.side_effect = RuntimeError(
        "sensitive fake path and synthetic-token"
    )
    with pytest.raises(OfficialLoginError) as error:
        await official_login.login(lambda _: None)
    assert str(error.value) == official_login.START_ERROR
    playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_failure_hides_underlying_exception(monkeypatch):
    page, context, browser, playwright = browser_fixture(monkeypatch)
    page.goto.side_effect = RuntimeError("synthetic-token in private redirect")
    with pytest.raises(OfficialLoginError) as error:
        await official_login.login(lambda _: None)
    assert str(error.value) == official_login.LOGIN_ERROR
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_timeout_closes_all_resources(monkeypatch):
    _, context, browser, playwright = browser_fixture(monkeypatch, cookies=[])
    with pytest.raises(OfficialLoginError) as error:
        await official_login.login(lambda _: None, timeout_seconds=0.02)
    assert str(error.value) == official_login.TIMEOUT_ERROR
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_cancellation_closes_all_resources(monkeypatch):
    _, context, browser, playwright = browser_fixture(monkeypatch, cookies=[])
    waiting = asyncio.Event()

    async def pending_cookies(_):
        waiting.set()
        await asyncio.Event().wait()

    context.cookies.side_effect = pending_cookies
    task = asyncio.create_task(official_login.login(lambda _: None))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_after_success_waits_for_cleanup_then_propagates(monkeypatch, cancel_count):
    _, context, browser, playwright = browser_fixture(monkeypatch)
    closing = asyncio.Event()
    allow_close = asyncio.Event()
    context_closed = asyncio.Event()

    async def close_context():
        closing.set()
        await allow_close.wait()
        context_closed.set()

    context.close.side_effect = close_context
    task = asyncio.create_task(official_login.login(lambda _: None))
    await closing.wait()
    context.cookies.assert_awaited_once()
    for _ in range(cancel_count):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context_closed.is_set()
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_emit_failure_is_safe_and_still_closes(monkeypatch):
    _, context, browser, playwright = browser_fixture(monkeypatch)
    emit = Mock(side_effect=[None, RuntimeError("synthetic-token")])
    with pytest.raises(OfficialLoginError) as error:
        await official_login.login(emit)
    assert str(error.value) == official_login.LOGIN_ERROR
    assert_closed(context, browser, playwright)


@pytest.mark.asyncio
async def test_close_failure_does_not_skip_remaining_cleanup(monkeypatch):
    _, context, browser, playwright = browser_fixture(monkeypatch)
    context.close.side_effect = RuntimeError("synthetic-token")
    assert await official_login.login(lambda _: None) == (USER, TOKEN)
    assert_closed(context, browser, playwright)
