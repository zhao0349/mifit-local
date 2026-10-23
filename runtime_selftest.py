"""Verify native runtime with a temporary synthetic credential and a blank page."""

import asyncio
import uuid

from keyring.backends.Windows import WinVaultKeyring
from playwright.async_api import async_playwright


async def browser_test():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="msedge", headless=True, chromium_sandbox=True,
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content("<title>MiFit synthetic runtime test</title>")
            assert await page.title() == "MiFit synthetic runtime test"
            await context.close()
        finally:
            await browser.close()
    print("System Edge isolated runtime: PASS")


def main():
    vault = WinVaultKeyring()
    service = "MiFit-Deployment-SelfTest-" + uuid.uuid4().hex
    secret = uuid.uuid4().hex
    written = False
    try:
        vault.set_password(service, "synthetic", secret)
        written = True
        assert vault.get_password(service, "synthetic") == secret
    finally:
        if written:
            vault.delete_password(service, "synthetic")
    print("Windows Credential Manager synthetic write/read/delete: PASS")
    asyncio.run(browser_test())


if __name__ == "__main__":
    main()
