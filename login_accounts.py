from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pyotp
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from threads_parser.accounts import (
    LOGIN_URL,
    READY,
    AccountConfig,
    AccountError,
    get_account,
    inspect_account_context,
    load_accounts,
    playwright_proxy,
    save_account_session,
)


def load_credentials(path: Path) -> dict[str, tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise AccountError(f"Cannot read credentials file: {path}") from exc

    credentials: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        fields = raw_line.split("\t")
        if len(fields) != 3 or not all(field.strip() for field in fields):
            raise AccountError(f"Invalid credentials format at line {line_number}.")
        username, password, totp_secret = (field.strip() for field in fields)
        key = username.lower()
        if key in credentials:
            raise AccountError(f"Duplicate credentials username at line {line_number}.")
        credentials[key] = (password, totp_secret.replace(" ", ""))
    return credentials


async def first_visible(page: Any, selectors: list[str], timeout: int = 15000) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        await page.wait_for_timeout(250)
    raise AccountError("Expected login field was not found.")


async def submit_login(page: Any, account: AccountConfig, password: str, totp_secret: str) -> None:
    username_input = await first_visible(
        page,
        ['input[name="username"]', 'input[autocomplete="username"]'],
    )
    password_input = await first_visible(
        page,
        ['input[name="password"]', 'input[autocomplete="current-password"]'],
    )
    await username_input.fill(account.username)
    await password_input.fill(password)
    await password_input.press("Enter")

    try:
        code_input = await first_visible(
            page,
            [
                'input[name="verificationCode"]',
                'input[name="approvals_code"]',
                'input[autocomplete="one-time-code"]',
                'input[aria-label*="code" i]',
            ],
            timeout=20000,
        )
    except AccountError:
        return

    try:
        code = pyotp.TOTP(totp_secret).now()
    except (ValueError, TypeError) as exc:
        raise AccountError("Invalid TOTP secret.") from exc
    await code_input.fill(code)
    await code_input.press("Enter")


async def login_one(browser: Any, account: AccountConfig, password: str, totp_secret: str) -> None:
    context = None
    try:
        context = await browser.new_context(
            proxy=playwright_proxy(account.proxy),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            await submit_login(page, account, password, totp_secret)
            await page.wait_for_timeout(8000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise AccountError("Login page failed; check the account proxy.") from exc

        status = await inspect_account_context(context)
        if status != READY:
            raise AccountError(f"Login was not completed: {status}. Manual login may be required.")
        await save_account_session(context, account.id)
    finally:
        if context:
            try:
                await context.close()
            except PlaywrightError:
                pass


async def run(args: argparse.Namespace) -> None:
    accounts = load_accounts(args.accounts_file)
    credentials = load_credentials(args.credentials_file)
    selected = [get_account(accounts, account_id) for account_id in args.account_ids]

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=args.headless)
        except PlaywrightError as exc:
            raise AccountError("Chromium launch failed.") from exc
        try:
            for account in selected:
                credential = credentials.get(account.username.lower())
                if credential is None:
                    raise AccountError(f"Credentials not found for parser account {account.id}.")
                print(f"Parser account {account.id}: logging in")
                await login_one(browser, account, *credential)
                print(f"Parser account {account.id}: ready")
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create parser account sessions from local credentials.")
    parser.add_argument("--accounts-file", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--account-ids", nargs="+", required=True)
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except AccountError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
