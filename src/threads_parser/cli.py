from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from pathlib import Path

from playwright.async_api import async_playwright

from .accounts import (
    PROXY_ERROR,
    READY,
    REAUTH_REQUIRED,
    AccountConfig,
    check_accounts,
    get_account,
    load_accounts,
    login_account,
)
from .errors import ProxyAccessError
from .proxies import clean_proxy, load_proxies
from .quarantine import (
    blocked_proxies,
    quarantine_account,
    quarantine_proxy,
    restore_account,
    restore_proxy,
)
from .runner import finish_traffic_report, run, run_account_batch, run_batch, run_search
from .search import (
    DEFAULT_SEARCH_MODE,
    DEFAULT_SEARCH_TYPE,
    SEARCH_MODES,
    SEARCH_TYPES,
    clean_search_query,
)
from .traffic import TrafficMonitor

DEFAULT_POST_LIMIT = 20

def clean_username(value: str) -> str:
    username = value.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", username):
        raise argparse.ArgumentTypeError("username can contain only letters, digits, dots and underscores")
    return username


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def clean_keyword(value: str) -> str:
    keyword = " ".join(value.split())
    if not keyword:
        raise argparse.ArgumentTypeError("keyword cannot be empty")
    return keyword


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parser for public Threads profiles and search results.")
    parser.add_argument("username", nargs="?", type=clean_username)
    parser.add_argument("--users-file", type=Path, help="JSON file with a list of usernames")
    parser.add_argument("--search", type=clean_search_query, metavar="QUERY", help="Parse Threads search results")
    parser.add_argument(
        "--search-mode",
        choices=SEARCH_MODES,
        help=f"Search by keyword or tag (default: {DEFAULT_SEARCH_MODE})",
    )
    parser.add_argument(
        "--search-type",
        choices=SEARCH_TYPES,
        help=f"Search result order (default: {DEFAULT_SEARCH_TYPE})",
    )
    crawl_group = parser.add_mutually_exclusive_group()
    crawl_group.add_argument(
        "--max-posts",
        type=positive_int,
        default=DEFAULT_POST_LIMIT,
        help=f"Number of posts to save for the target (default: {DEFAULT_POST_LIMIT})",
    )
    crawl_group.add_argument("--all-posts", action="store_true", help="Scroll until the target feed is exhausted")
    parser.add_argument(
        "--keywords",
        nargs="+",
        type=clean_keyword,
        metavar="KEYWORD",
        help="Save posts whose text contains at least one keyword or phrase",
    )
    parser.add_argument("--proxy", type=clean_proxy, help="Optional HTTP proxy URL, e.g. http://user:pass@host:port")
    parser.add_argument("--proxies-file", type=Path, help="Proxy list, one proxy per line")
    parser.add_argument("--profiles-per-proxy", type=positive_int, default=3)
    parser.add_argument(
        "--accounts-per-proxy",
        dest="profiles_per_proxy",
        type=positive_int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--workers", type=positive_int, default=3)
    parser.add_argument("--incremental", action="store_true", help="Keep saved posts and download only new media")
    parser.add_argument("--accounts-file", type=Path, help="Parser account configuration JSON")
    parser.add_argument(
        "--profiles-per-account",
        type=positive_int,
        help="Maximum target profiles attempted by one parser account in this run",
    )
    parser.add_argument("--login-account", metavar="ACCOUNT_ID", help="Open an interactive login for a parser account")
    parser.add_argument("--check-accounts", action="store_true", help="Check parser account sessions and proxies")
    parser.add_argument("--traffic-report", type=Path, help="Write proxy traffic report JSON to this path")
    args = parser.parse_args()
    if args.all_posts:
        args.max_posts = None

    if args.login_account and args.check_accounts:
        parser.error("provide either --login-account or --check-accounts")
    if args.login_account or args.check_accounts:
        if not args.accounts_file:
            parser.error("--accounts-file is required for parser account management")
        if args.username or args.users_file or args.search:
            parser.error("target profiles are not allowed with account management commands")
        if args.proxy or args.proxies_file:
            parser.error("anonymous proxy options are not allowed with account management commands")
        if args.keywords:
            parser.error("keywords are not allowed with account management commands")
        if args.search_mode is not None or args.search_type is not None:
            parser.error("search options are not allowed with account management commands")
        return args

    if sum(value is not None for value in (args.username, args.users_file, args.search)) != 1:
        parser.error("provide exactly one of username, --users-file or --search")
    if args.proxy and args.proxies_file:
        parser.error("provide either --proxy or --proxies-file")
    if args.accounts_file and (args.proxy or args.proxies_file):
        parser.error("parser accounts cannot be combined with anonymous proxy options")
    if args.profiles_per_account and not args.accounts_file:
        parser.error("--profiles-per-account requires --accounts-file")
    if args.search is None and (args.search_mode is not None or args.search_type is not None):
        parser.error("--search-mode and --search-type require --search")
    args.search_mode = args.search_mode or DEFAULT_SEARCH_MODE
    args.search_type = args.search_type or DEFAULT_SEARCH_TYPE
    return args


def load_usernames(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except OSError as exc:
        raise RuntimeError(f"Cannot read users file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in users file: {path}") from exc

    values = data.get("users") if isinstance(data, dict) else data
    if not isinstance(values, list):
        raise RuntimeError('Users file must contain ["user1", "user2"] or {"users": [...]}.')

    usernames: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise RuntimeError("Every username in users file must be a string.")
        try:
            username = clean_username(value)
        except argparse.ArgumentTypeError as exc:
            raise RuntimeError(f"Invalid username in users file: {value}") from exc
        if username not in usernames:
            usernames.append(username)

    if not usernames:
        raise RuntimeError("Users file is empty.")
    return usernames

async def run_login_command(account: AccountConfig) -> None:
    async with async_playwright() as playwright:
        await login_account(playwright, account)


async def run_check_accounts_command(accounts: list[AccountConfig]) -> dict[str, str]:
    async with async_playwright() as playwright:
        return await check_accounts(playwright, accounts)


def finish_report_after_run(
    traffic_monitor: TrafficMonitor,
    report_path: Path,
    operation_failed: bool,
) -> None:
    try:
        finish_traffic_report(traffic_monitor, report_path)
    except OSError as exc:
        if operation_failed:
            print(f"Traffic report error: {exc}")
            return
        raise RuntimeError(f"Cannot write traffic report: {report_path}") from exc


def main() -> None:
    args = parse_args()
    try:
        if args.accounts_file:
            accounts = load_accounts(args.accounts_file)
            if args.login_account:
                account = get_account(accounts, args.login_account)
                asyncio.run(run_login_command(account))
                restore_account(account.id)
                restore_proxy(account.proxy)
                print(f"Parser account {account.id}: ready")
                return
            if args.check_accounts:
                statuses = asyncio.run(run_check_accounts_command(accounts))
                for account in accounts:
                    status = statuses[account.id]
                    if status == READY:
                        restore_account(account.id)
                        restore_proxy(account.proxy)
                    elif status in {REAUTH_REQUIRED, PROXY_ERROR}:
                        quarantine_account(account.id, status)
                        if status == PROXY_ERROR:
                            quarantine_proxy(account.proxy, status)
                    print(f"{account.id}: {status}")
                if any(account.enabled and statuses[account.id] != READY for account in accounts):
                    raise SystemExit(1)
                return

            if args.search:
                targets = [args.search]
            elif args.username:
                targets = [args.username]
            else:
                targets = load_usernames(args.users_file)
            failed, unfinished = asyncio.run(
                run_account_batch(
                    targets,
                    accounts,
                    args.max_posts,
                    args.workers,
                    args.incremental,
                    args.profiles_per_account,
                    args.traffic_report,
                    args.keywords,
                    args.search is not None,
                    args.search_mode,
                    args.search_type,
                )
            )
            if failed or unfinished:
                raise SystemExit(1)
            return

        quarantined_proxies = blocked_proxies()
        proxies = load_proxies(args.proxies_file) if args.proxies_file else None
        if proxies is not None:
            proxies = [proxy for proxy in proxies if proxy not in quarantined_proxies]
            if not proxies:
                raise RuntimeError("No usable proxies remain; see blocked_proxies.json.")
        if args.proxy in quarantined_proxies:
            raise RuntimeError("The selected proxy is quarantined; see blocked_proxies.json.")
        if args.search:
            selected_proxy = random.choice(proxies) if proxies else args.proxy
            traffic_monitor = TrafficMonitor() if args.traffic_report else None
            operation_failed = False
            try:
                try:
                    asyncio.run(
                        run_search(
                            args.search,
                            args.max_posts,
                            selected_proxy,
                            incremental=args.incremental,
                            keywords=args.keywords,
                            search_mode=args.search_mode,
                            search_type=args.search_type,
                            traffic_monitor=traffic_monitor,
                            account_id="anonymous" if traffic_monitor is not None else None,
                        )
                    )
                except ProxyAccessError:
                    if selected_proxy:
                        quarantine_proxy(selected_proxy, PROXY_ERROR)
                    raise RuntimeError("Proxy was quarantined after an access failure.") from None
            except BaseException:
                operation_failed = True
                raise
            finally:
                if traffic_monitor is not None:
                    finish_report_after_run(traffic_monitor, args.traffic_report, operation_failed)
            return
        if args.username:
            selected_proxy = random.choice(proxies) if proxies else args.proxy
            traffic_monitor = TrafficMonitor() if args.traffic_report else None
            operation_failed = False
            try:
                try:
                    asyncio.run(
                        run(
                            args.username,
                            args.max_posts,
                            selected_proxy,
                            incremental=args.incremental,
                            keywords=args.keywords,
                            traffic_monitor=traffic_monitor,
                            account_id="anonymous" if traffic_monitor is not None else None,
                        )
                    )
                except ProxyAccessError:
                    if selected_proxy:
                        quarantine_proxy(selected_proxy, PROXY_ERROR)
                    raise RuntimeError("Proxy was quarantined after an access failure.") from None
            except BaseException:
                operation_failed = True
                raise
            finally:
                if traffic_monitor is not None:
                    finish_report_after_run(traffic_monitor, args.traffic_report, operation_failed)
            return

        usernames = load_usernames(args.users_file)
        failed = asyncio.run(
            run_batch(
                usernames,
                args.max_posts,
                args.proxy,
                proxies,
                args.profiles_per_proxy,
                args.workers,
                args.incremental,
                args.keywords,
                traffic_report_path=args.traffic_report,
            )
        )
        if failed:
            raise SystemExit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
