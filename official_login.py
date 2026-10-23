"""Local, user-driven Xiaomi login through a fresh, visible Edge session.

This is an additional integration, not an upstream-supported authorization flow.
Whether QR login is offered and whether the required cookies are issued is
controlled by Xiaomi. No password, verification code, or page content is read.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import Any

LOGIN_URL = "https://account.xiaomi.com/"
COOKIE_URLS = (LOGIN_URL, "https://account.xiaomi.com/pass/")
_ALLOWED_DOMAINS = {"account.xiaomi.com", ".account.xiaomi.com", ".xiaomi.com"}
_APPLICABLE_PATHS = {"/", "/pass", "/pass/"}
_NAMES = {"userId", "passToken"}

START_ERROR = "无法启动独立的 Edge 登录窗口，请确认已安装 Microsoft Edge 后重试。"
CLOSED_ERROR = "登录窗口已关闭，尚未完成账号连接。"
TIMEOUT_ERROR = "等待登录超时。请重试并在小米官网完成登录和验证。"
LOGIN_ERROR = "小米官网登录未完成，请检查网络后重试。"


class OfficialLoginError(RuntimeError):
    """A fixed, credential-free message suitable for display in the local UI."""


def _valid_value(value: Any) -> bool:
    # RFC cookie-octet characters only: this will later enter a Cookie header.
    return (
        isinstance(value, str)
        and bool(value)
        and all(33 <= ord(char) <= 126 and char not in '";,\\' for char in value)
    )


def select_credentials(
    cookies: Iterable[Mapping[str, Any]], *, now: float | None = None
) -> tuple[str, str] | None:
    """Select an unambiguous pair from official HTTPS URL-filtered cookies.

    The caller must request cookies for COOKIE_URLS, never the entire context.
    Chromium applies URL/domain/path matching; exact accepted domains and paths
    are checked again here. Secure=False cookies are still valid for HTTPS.
    Names may have different scopes, but each must have exactly one distinct
    applicable value. Expiry -1 means a session cookie; other expiry values
    must be finite and in the future.
    """
    current_time = time.time() if now is None else now
    candidates: dict[str, set[str]] = {name: set() for name in _NAMES}
    for cookie in cookies:
        domain = cookie.get("domain")
        name = cookie.get("name")
        value = cookie.get("value")
        expiry = cookie.get("expires")
        path = cookie.get("path")
        if (
            not isinstance(domain, str)
            or domain not in _ALLOWED_DOMAINS
            or not isinstance(name, str)
            or name not in _NAMES
            or not isinstance(path, str)
            or path not in _APPLICABLE_PATHS
            or not _valid_value(value)
            or type(expiry) not in (int, float)
            or not math.isfinite(expiry)
            or (expiry != -1 and expiry <= current_time)
        ):
            continue
        candidates[name].add(value)

    users = candidates["userId"]
    tokens = candidates["passToken"]
    # Never guess precedence or try combinations when scopes disagree.
    if len(users) == len(tokens) == 1:
        return next(iter(users)), next(iter(tokens))
    return None


def _playwright_factory():
    # Keep pure cookie validation usable without importing the optional browser
    # dependency. Installation is managed by the local application setup.
    from playwright.async_api import async_playwright

    return async_playwright()


def _was_closed(browser: Any, page: Any) -> bool:
    return bool(
        (browser is not None and not browser.is_connected())
        or (page is not None and page.is_closed())
    )


async def _cleanup(context: Any, browser: Any, playwright: Any) -> None:
    # Clean every resource even if an earlier close fails. Never include
    # Playwright exceptions in logs or UI: URLs can contain session material.
    for resource, operation in (
        (context, "close"),
        (browser, "close"),
        (playwright, "stop"),
    ):
        if resource is not None:
            with suppress(Exception):
                await asyncio.wait_for(getattr(resource, operation)(), timeout=5)


async def login(
    emit: Callable[[str], None], timeout_seconds: float = 600
) -> tuple[str, str]:
    """Let the user log in on Xiaomi's site; return secrets only to the caller.

    The caller must store the returned pair locally without printing it. A
    cancelled task propagates CancelledError after closing this browser session.
    No existing browser profile or persistent browser context is accessed.
    """
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("登录等待时间必须为有限正数。")

    playwright = browser = context = page = None
    stage = "start"
    try:
        async with asyncio.timeout(timeout_seconds):
            emit("正在打开独立的 Edge 窗口，连接小米账号官网。")
            playwright = await _playwright_factory().start()
            browser = await playwright.chromium.launch(
                channel="msedge",
                headless=False,
                chromium_sandbox=True,
                timeout=30000,
            )
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()
            stage = "login"
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            emit("请在小米官网亲自登录并完成验证；若官网提供二维码，可自行扫码。")
            emit("连接成功后窗口会自动关闭，账号凭证只交给本机程序保存。")
            while True:
                if _was_closed(browser, page):
                    raise OfficialLoginError(CLOSED_ERROR)
                # This URL filter excludes cookies belonging to unrelated sites.
                cookies = await context.cookies(list(COOKIE_URLS))
                credentials = select_credentials(cookies)
                del cookies
                if credentials is not None:
                    emit("已从小米官网登录会话取得所需凭证，正在关闭登录窗口。")
                    return credentials
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        raise
    except OfficialLoginError:
        raise
    except TimeoutError:
        raise OfficialLoginError(TIMEOUT_ERROR) from None
    except Exception:
        if _was_closed(browser, page):
            raise OfficialLoginError(CLOSED_ERROR) from None
        raise OfficialLoginError(START_ERROR if stage == "start" else LOGIN_ERROR) from None
    finally:
        # Cancellation may arrive for the first time after credentials have
        # already been found, while close() is awaiting the browser. Protect
        # that same cleanup task until it finishes, then preserve cancellation.
        cleanup_task = asyncio.create_task(_cleanup(context, browser, playwright))
        cancellation = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
        cleanup_task.result()
        if cancellation is not None:
            raise cancellation
