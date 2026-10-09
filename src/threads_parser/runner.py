from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .accounts import (
    PROXY_ERROR,
    READY,
    AccountConfig,
    AccountRuntime,
    AccountUnavailableError,
    TargetJob,
    assign_targets_fair,
    create_account_context,
    inspect_account_context,
    local_session_status,
)
from .browser import (
    configure_scan_context,
    launch_browser,
    load_public_profile,
    load_public_profile_in_browser,
    load_public_profile_in_context,
)
from .errors import ProxyAccessError, TargetProfileError
from .media import download_media, prune_unreferenced_media
from .parser import extract_posts, filter_posts_by_keywords, merge_posts
from .quarantine import blocked_account_ids, blocked_proxies, quarantine_account, quarantine_proxy
from .search import (
    DEFAULT_SEARCH_MODE,
    DEFAULT_SEARCH_TYPE,
    extract_search_posts,
    load_search_results,
    load_search_results_in_browser,
    load_search_results_in_context,
    search_output_dir,
    search_result_connections,
)
from .storage import atomic_write_json, load_saved_posts
from .traffic import TrafficMonitor, default_traffic_report_path

async def run(
    username: str,
    post_limit: int | None,
    proxy: str | None,
    browser: Any | None = None,
    incremental: bool = False,
    context: Any | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
    keywords: list[str] | None = None,
) -> None:
    account_id = account_id or ("anonymous" if traffic_monitor is not None else None)
    output_dir = Path("output") / username
    saved_posts = load_saved_posts(output_dir / "posts.json") if incremental else []
    saved_posts = filter_posts_by_keywords(saved_posts, keywords)
    known_permalinks = (
        {
            urlparse(post["permalink"]).path.rstrip("/")
            for post in saved_posts
            if isinstance(post.get("permalink"), str)
        }
        if incremental and post_limit is not None
        else set()
    )

    if browser is not None and context is not None:
        raise RuntimeError("Browser and context cannot be provided together.")
    if context is not None:
        raw_items, dom_posts, user_agent = await load_public_profile_in_context(
            context,
            username,
            post_limit,
            known_permalinks,
            authenticated=True,
            keywords=keywords,
            traffic_monitor=traffic_monitor,
            account_id=account_id,
        )
    elif browser is None:
        raw_items, dom_posts, user_agent = await load_public_profile(
            username,
            post_limit,
            proxy,
            known_permalinks,
            keywords,
            traffic_monitor,
            account_id,
        )
    else:
        raw_items, dom_posts, user_agent = await load_public_profile_in_browser(
            browser,
            username,
            post_limit,
            known_permalinks,
            proxied=proxy is not None,
            keywords=keywords,
            traffic_monitor=traffic_monitor,
            account_id=account_id,
        )
    fetched_posts = merge_posts(extract_posts(raw_items, username), dom_posts, prefer_incoming_text=False)
    if not fetched_posts:
        raise TargetProfileError(
            "Posts not found. The target profile may be restricted or access may be blocked.",
            retryable=True,
        )
    found_posts = len(fetched_posts)
    fetched_posts = filter_posts_by_keywords(fetched_posts, keywords)
    if post_limit is not None:
        fetched_posts = fetched_posts[:post_limit]
    if not keywords:
        found_posts = len(fetched_posts)

    posts = merge_posts(fetched_posts, saved_posts, prefer_incoming_text=False) if incremental else fetched_posts
    new_posts = max(0, len(posts) - len(saved_posts))

    output_dir.mkdir(parents=True, exist_ok=True)
    available_media, downloaded_media = await download_media(
        posts,
        output_dir,
        proxy,
        user_agent,
        context.request if context is not None else None,
        traffic_monitor,
        account_id,
    )

    atomic_write_json(output_dir / "posts.json", posts)
    removed_media = prune_unreferenced_media(output_dir, posts) if incremental else 0

    print(f"Profile: {username}")
    print(f"Posts found: {found_posts}")
    if keywords:
        print(f"Posts matched: {len(fetched_posts)}")
    if incremental:
        print(f"New posts: {new_posts}")
        print(f"Posts saved: {len(posts)}")
    print(f"Media downloaded: {downloaded_media}")
    print(f"Media available: {available_media}")
    if removed_media:
        print(f"Stale media removed: {removed_media}")
    print(f"Output: {output_dir.as_posix()}")


async def run_search(
    query: str,
    post_limit: int | None,
    proxy: str | None,
    browser: Any | None = None,
    incremental: bool = False,
    context: Any | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
    keywords: list[str] | None = None,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> None:
    account_id = account_id or ("anonymous" if traffic_monitor is not None else None)
    output_dir = search_output_dir(query, search_mode, search_type)
    saved_posts = load_saved_posts(output_dir / "posts.json") if incremental else []
    saved_posts = filter_posts_by_keywords(saved_posts, keywords)

    if browser is not None and context is not None:
        raise RuntimeError("Browser and context cannot be provided together.")
    if context is not None:
        raw_items, user_agent, actual_mode, actual_type = await load_search_results_in_context(
            context,
            query,
            post_limit,
            authenticated=True,
            keywords=keywords,
            search_mode=search_mode,
            search_type=search_type,
            traffic_monitor=traffic_monitor,
            account_id=account_id,
        )
    elif browser is None:
        raw_items, user_agent, actual_mode, actual_type = await load_search_results(
            query,
            post_limit,
            proxy,
            keywords,
            search_mode,
            search_type,
            traffic_monitor,
            account_id,
        )
    else:
        raw_items, user_agent, actual_mode, actual_type = await load_search_results_in_browser(
            browser,
            query,
            post_limit,
            proxied=proxy is not None,
            keywords=keywords,
            search_mode=search_mode,
            search_type=search_type,
            traffic_monitor=traffic_monitor,
            account_id=account_id,
        )

    if not search_result_connections(raw_items):
        raise RuntimeError("Threads search results payload was not found.")
    fetched_posts = extract_search_posts(raw_items)
    found_posts = len(fetched_posts)
    fetched_posts = filter_posts_by_keywords(fetched_posts, keywords)
    if post_limit is not None:
        fetched_posts = fetched_posts[:post_limit]
    if not keywords:
        found_posts = len(fetched_posts)
    posts = merge_posts(fetched_posts, saved_posts, prefer_incoming_text=False) if incremental else fetched_posts
    new_posts = max(0, len(posts) - len(saved_posts))

    output_dir.mkdir(parents=True, exist_ok=True)
    available_media, downloaded_media = await download_media(
        posts,
        output_dir,
        proxy,
        user_agent,
        context.request if context is not None else None,
        traffic_monitor,
        account_id,
    )
    atomic_write_json(output_dir / "posts.json", posts)
    removed_media = prune_unreferenced_media(output_dir, posts) if incremental else 0

    print(f"Search: {query}")
    print(f"Search mode: {search_mode}")
    print(f"Search type: {search_type}")
    if (actual_mode, actual_type) != (search_mode, search_type):
        print(f"Actual search mode: {actual_mode}")
        print(f"Actual search type: {actual_type}")
    print(f"Posts found: {found_posts}")
    if keywords:
        print(f"Posts matched: {len(fetched_posts)}")
    if incremental:
        print(f"New posts: {new_posts}")
        print(f"Posts saved: {len(posts)}")
    print(f"Media downloaded: {downloaded_media}")
    print(f"Media available: {available_media}")
    if removed_media:
        print(f"Stale media removed: {removed_media}")
    print(f"Output: {output_dir.as_posix()}")


def quarantine_account_runtime(runtime: AccountRuntime, status: str) -> None:
    quarantine_account(runtime.config.id, status)
    if status == PROXY_ERROR:
        quarantine_proxy(runtime.config.proxy, status)


async def process_account_jobs(
    browser: Any,
    runtime: AccountRuntime,
    jobs: list[TargetJob],
    post_limit: int | None,
    incremental: bool,
    semaphore: asyncio.Semaphore,
    traffic_monitor: TrafficMonitor | None = None,
    keywords: list[str] | None = None,
    is_search: bool = False,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> tuple[list[TargetJob], list[str], list[str]]:
    pending: list[TargetJob] = []
    failed: list[str] = []
    completed: list[str] = []

    async with semaphore:
        context = None
        try:
            context = await create_account_context(browser, runtime.config)
            runtime.context = context
            await configure_scan_context(context)
            if traffic_monitor is not None:
                traffic_monitor.attach_context(runtime.config.id, context)
            status = await inspect_account_context(context)
            if status != READY:
                runtime.disable(status)
                quarantine_account_runtime(runtime, status)
                print(f"Parser account {runtime.config.id}: {status}")
                return jobs, failed, completed

            for index, job in enumerate(jobs):
                runtime.record_attempt()
                job.record_attempt(runtime.config.id)
                try:
                    if is_search:
                        await run_search(
                            job.username,
                            post_limit,
                            None,
                            incremental=incremental,
                            context=context,
                            traffic_monitor=traffic_monitor,
                            account_id=runtime.config.id,
                            keywords=keywords,
                            search_mode=search_mode,
                            search_type=search_type,
                        )
                    else:
                        await run(
                            job.username,
                            post_limit,
                            None,
                            incremental=incremental,
                            context=context,
                            traffic_monitor=traffic_monitor,
                            account_id=runtime.config.id,
                            keywords=keywords,
                        )
                    completed.append(job.username)
                except AccountUnavailableError as exc:
                    runtime.disable(exc.status)
                    quarantine_account_runtime(runtime, exc.status)
                    print(f"{'Search query' if is_search else 'Target profile'}: {job.username}")
                    print(f"Parser account {runtime.config.id}: {exc.status}")
                    if job.can_retry:
                        pending.append(job)
                    else:
                        failed.append(job.username)
                    pending.extend(jobs[index + 1 :])
                    break
                except TargetProfileError as exc:
                    print(f"Target profile: {job.username}")
                    print(f"Error: {exc}")
                    if exc.retryable and job.can_retry:
                        pending.append(job)
                    else:
                        failed.append(job.username)
                except RuntimeError as exc:
                    print(f"{'Search query' if is_search else 'Target profile'}: {job.username}")
                    print(f"Error: {exc}")
                    if job.can_retry:
                        pending.append(job)
                    else:
                        failed.append(job.username)
        except AccountUnavailableError as exc:
            runtime.disable(exc.status)
            quarantine_account_runtime(runtime, exc.status)
            print(f"Parser account {runtime.config.id}: {exc.status}")
            pending.extend(jobs)
        finally:
            runtime.context = None
            if context:
                try:
                    await context.close()
                except PlaywrightError:
                    pass
    return pending, failed, completed


async def run_account_batch(
    usernames: list[str],
    accounts: list[AccountConfig],
    post_limit: int | None,
    workers: int,
    incremental: bool,
    profiles_per_account: int | None = None,
    traffic_report_path: Path | None = None,
    keywords: list[str] | None = None,
    is_search: bool = False,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> tuple[list[str], list[str]]:
    traffic_monitor = TrafficMonitor()
    traffic_report_path = traffic_report_path or default_traffic_report_path()
    label = "Search queries" if is_search else "Target profiles"

    runtimes = [AccountRuntime(account, run_limit=profiles_per_account) for account in accounts if account.enabled]
    if not runtimes:
        print(f"{label} completed: 0")
        print(f"{label} failed: 0")
        print(f"{label} unfinished: {len(usernames)}")
        print("Error: no enabled parser accounts.")
        print(f"Unfinished targets: {', '.join(usernames)}")
        return [], usernames

    quarantined_accounts = blocked_account_ids()
    quarantined_proxies = blocked_proxies()
    for runtime in runtimes:
        if runtime.config.id.lower() in quarantined_accounts or runtime.config.proxy in quarantined_proxies:
            runtime.disable("quarantined")
            print(f"Parser account {runtime.config.id}: quarantined")
            continue
        status = local_session_status(runtime.config)
        if status != READY:
            runtime.disable(status)
            print(f"Parser account {runtime.config.id}: {status}")
    if not any(runtime.healthy for runtime in runtimes):
        print()
        print(f"{label} completed: 0")
        print(f"{label} failed: 0")
        print(f"{label} unfinished: {len(usernames)}")
        print("Error: no ready parser account sessions.")
        print(f"Unfinished targets: {', '.join(usernames)}")
        return [], usernames

    pending = [TargetJob(username) for username in usernames]
    failed: list[str] = []
    completed: list[str] = []
    unfinished: list[str] = []

    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, None)
        semaphore = asyncio.Semaphore(min(workers, len(runtimes)))
        try:
            while pending:
                retryable = [job for job in pending if job.can_retry]
                failed.extend(job.username for job in pending if not job.can_retry)
                groups, unassigned = assign_targets_fair(retryable, runtimes)
                if not groups:
                    unfinished = [job.username for job in unassigned]
                    break

                results = await asyncio.gather(
                    *(
                        process_account_jobs(
                            browser,
                            runtime,
                            jobs,
                            post_limit,
                            incremental,
                            semaphore,
                            traffic_monitor,
                            keywords,
                            is_search,
                            search_mode,
                            search_type,
                        )
                        for runtime, jobs in groups
                    )
                )
                pending = list(unassigned)
                for retry_jobs, failed_targets, completed_targets in results:
                    pending.extend(retry_jobs)
                    failed.extend(failed_targets)
                    completed.extend(completed_targets)
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass

    failed = list(dict.fromkeys(failed))
    unfinished = list(dict.fromkeys(unfinished))
    print()
    print(f"{label} completed: {len(set(completed))}")
    print(f"{label} failed: {len(failed)}")
    print(f"{label} unfinished: {len(unfinished)}")
    if unfinished:
        print("Error: no healthy parser account capacity remains.")
        print(f"Unfinished targets: {', '.join(unfinished)}")
    traffic_report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(traffic_report_path, traffic_monitor.report())
    traffic_monitor.print_summary(traffic_report_path)
    return failed, unfinished


def build_proxy_groups(
    usernames: list[str],
    proxies: list[str] | None,
    profiles_per_proxy: int,
) -> list[tuple[list[str], str | None]]:
    if not proxies:
        return [(usernames, None)]

    groups: list[tuple[list[str], str | None]] = []
    available: list[str] = []
    for start in range(0, len(usernames), profiles_per_proxy):
        if not available:
            available = proxies.copy()
            random.shuffle(available)
        groups.append((usernames[start : start + profiles_per_proxy], available.pop()))
    return groups


async def run_batch(
    usernames: list[str],
    post_limit: int | None,
    proxy: str | None,
    proxies: list[str] | None = None,
    profiles_per_proxy: int = 3,
    workers: int = 3,
    incremental: bool = False,
    keywords: list[str] | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    traffic_report_path: Path | None = None,
) -> list[str]:
    if traffic_report_path is not None and traffic_monitor is None:
        traffic_monitor = TrafficMonitor()
    failed: list[str] = []
    groups = build_proxy_groups(usernames, proxies, profiles_per_proxy)
    queue: asyncio.Queue[tuple[list[str], str | None]] = asyncio.Queue()
    for group in groups:
        queue.put_nowait(group)

    async with async_playwright() as playwright:
        async def process_groups() -> None:
            while True:
                try:
                    group_usernames, selected_proxy = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                active_proxy = selected_proxy or proxy
                try:
                    browser = await launch_browser(playwright, active_proxy)
                except RuntimeError as exc:
                    for username in group_usernames:
                        failed.append(username)
                        print(f"Profile: {username}")
                        print(f"Error: {exc}")
                    queue.task_done()
                    continue

                try:
                    for index, username in enumerate(group_usernames):
                        try:
                            await run(
                                username,
                                post_limit,
                                active_proxy,
                                browser,
                                incremental,
                                keywords=keywords,
                                traffic_monitor=traffic_monitor,
                                account_id="anonymous" if traffic_monitor is not None else None,
                            )
                        except ProxyAccessError:
                            if active_proxy:
                                quarantine_proxy(active_proxy, PROXY_ERROR)
                            for pending_username in group_usernames[index:]:
                                failed.append(pending_username)
                                print(f"Profile: {pending_username}")
                                print("Error: proxy was quarantined after an access failure.")
                            break
                        except RuntimeError as exc:
                            failed.append(username)
                            print(f"Profile: {username}")
                            print(f"Error: {exc}")
                finally:
                    try:
                        await browser.close()
                    except PlaywrightError:
                        pass
                    queue.task_done()

        worker_count = min(workers if proxies else 1, len(groups))
        await asyncio.gather(*(process_groups() for _ in range(worker_count)))

    print()
    print(f"Profiles processed: {len(usernames)}")
    print(f"Profiles failed: {len(failed)}")
    if traffic_monitor is not None and traffic_report_path is not None:
        finish_traffic_report(traffic_monitor, traffic_report_path)
    return failed


def finish_traffic_report(traffic_monitor: TrafficMonitor, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(report_path, traffic_monitor.report())
    traffic_monitor.print_summary(report_path)
