from __future__ import annotations
import argparse, asyncio, hashlib, json, os, random, re, tempfile, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

BASE_URL = "https://www.threads.com"
SEARCH_MODES = ("keyword", "tag")
SEARCH_TYPES = ("top", "recent")
DEFAULT_SEARCH_MODE = "tag"
DEFAULT_SEARCH_TYPE = "top"
FULL_CRAWL_MAX_SCROLLS = 1000
from .parser import *
def clean_search_query(value: str) -> str:
    query = " ".join(value.split())
    if not query:
        raise argparse.ArgumentTypeError("search query cannot be empty")
    return query


def search_url(query: str, search_mode: str, search_type: str) -> str:
    url = f"{BASE_URL}/search/?q={quote(query, safe='')}"
    if search_mode == "tag":
        url += "&serp_type=tags"
    if search_type == "recent":
        url += "&filter=recent"
    return url


def search_mode_from_url(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    mode = "tag" if query.get("serp_type") == ["tags"] else "keyword"
    search_type = "recent" if query.get("filter") == ["recent"] else "top"
    return mode, search_type


def search_result_connections(raw_items: list[Any]) -> list[dict[str, Any]]:
    connections: list[dict[str, Any]] = []
    for raw in raw_items:
        for item in walk_json(raw):
            result = item.get("searchResults")
            if isinstance(result, dict) and isinstance(result.get("edges"), list):
                connections.append(result)
    return connections


def extract_search_posts(raw_items: list[Any]) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for result in search_result_connections(raw_items):
        for edge in result["edges"]:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            thread = node.get("thread") if isinstance(node, dict) else None
            thread_items = thread.get("thread_items") if isinstance(thread, dict) else None
            if not isinstance(thread_items, list) or not thread_items or not isinstance(thread_items[0], dict):
                continue
            source = thread_items[0].get("post")
            if not isinstance(source, dict):
                continue
            post = normalize_post(source, "")
            if post and post["username"] and post["permalink"]:
                posts.append(post)
    return merge_posts([], posts)


def search_has_next_page(raw_items: list[Any]) -> bool | None:
    for raw in reversed(raw_items):
        latest: bool | None = None
        for item in walk_json(raw):
            result = item.get("searchResults")
            page_info = result.get("page_info") if isinstance(result, dict) else None
            has_next_page = page_info.get("has_next_page") if isinstance(page_info, dict) else None
            if isinstance(has_next_page, bool):
                latest = has_next_page
        if latest is not None:
            return latest
    return None


def safe_filename(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "post").strip("._")
    return cleaned[:80] or "post"


def search_output_dir(
    query: str,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> Path:
    expected = query.replace(" ", "_")
    slug = safe_filename(query)
    if slug != expected[:80] or len(expected) > 80:
        digest = hashlib.sha256(query.casefold().encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[:71]}-{digest}"
    return Path("output") / "search" / search_mode / search_type / slug


async def scroll_search(
    page: Any,
    post_limit: int | None,
    raw_items: list[Any],
    keywords: list[str] | None = None,
    response_event: asyncio.Event | None = None,
) -> None:
    stalled_at_bottom = 0
    await page.mouse.move(640, 700)

    for _ in range(FULL_CRAWL_MAX_SCROLLS):
        posts = extract_search_posts(raw_items)
        if post_limit is not None and len(filter_posts_by_keywords(posts, keywords)) >= post_limit:
            break

        before_state = await page.evaluate(
            "() => ({ top: document.documentElement.scrollTop, height: document.documentElement.scrollHeight, viewport: innerHeight })"
        )
        before_count = len(posts)
        before_height = before_state["height"]
        at_bottom = before_state["top"] + before_state["viewport"] >= before_height - 100
        if at_bottom and search_has_next_page(raw_items) is False:
            break

        if response_event is not None:
            response_event.clear()
        await page.mouse.wheel(0, random.randint(1400, 1900))
        await page.wait_for_timeout(random.randint(350, 650))

        current_state = await page.evaluate(
            "() => ({ top: document.documentElement.scrollTop, height: document.documentElement.scrollHeight, viewport: innerHeight })"
        )
        near_bottom = current_state["top"] + current_state["viewport"] >= current_state["height"] - 1800
        if near_bottom and response_event is not None and not response_event.is_set():
            try:
                await asyncio.wait_for(response_event.wait(), timeout=3.5)
            except asyncio.TimeoutError:
                pass
            await page.wait_for_timeout(random.randint(250, 450))

        posts = extract_search_posts(raw_items)
        after_state = await page.evaluate(
            "() => ({ top: document.documentElement.scrollTop, height: document.documentElement.scrollHeight, viewport: innerHeight })"
        )
        at_bottom = after_state["top"] + after_state["viewport"] >= after_state["height"] - 100
        progressed = len(posts) > before_count or after_state["height"] > before_height
        stalled_at_bottom = stalled_at_bottom + 1 if at_bottom and not progressed else 0
        if at_bottom and search_has_next_page(raw_items) is False:
            break
        if stalled_at_bottom >= 4:
            break


async def load_search_results(
    query: str,
    post_limit: int | None,
    proxy: str | None,
    keywords: list[str] | None = None,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> tuple[list[Any], str, str, str]:
    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, proxy)
        try:
            return await load_search_results_in_browser(
                browser,
                query,
                post_limit,
                proxied=proxy is not None,
                keywords=keywords,
                search_mode=search_mode,
                search_type=search_type,
            )
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass


async def load_search_results_in_browser(
    browser: Any,
    query: str,
    post_limit: int | None,
    proxied: bool = False,
    keywords: list[str] | None = None,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> tuple[list[Any], str, str, str]:
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    try:
        await configure_scan_context(context)
        return await load_search_results_in_context(
            context,
            query,
            post_limit,
            proxied=proxied,
            keywords=keywords,
            search_mode=search_mode,
            search_type=search_type,
        )
    finally:
        try:
            await context.close()
        except PlaywrightError:
            pass


async def load_search_results_in_context(
    context: Any,
    query: str,
    post_limit: int | None,
    authenticated: bool = False,
    proxied: bool = False,
    keywords: list[str] | None = None,
    search_mode: str = DEFAULT_SEARCH_MODE,
    search_type: str = DEFAULT_SEARCH_TYPE,
) -> tuple[list[Any], str, str, str]:
    raw_items: list[Any] = []
    response_tasks: list[asyncio.Task[None]] = []
    response_event = asyncio.Event()
    account_response_statuses: list[int] = []
    proxy_response_statuses: list[int] = []
    url = search_url(query, search_mode, search_type)
    page = None

    try:
        try:
            page = await context.new_page()
        except PlaywrightError as exc:
            if authenticated:
                raise AccountUnavailableError("Parser account context is unavailable.", REAUTH_REQUIRED) from exc
            raise RuntimeError("Browser context is unavailable.") from exc

        async def collect_search_response(response: Any) -> None:
            before = len(raw_items)
            await collect_response_json(response, raw_items)
            if len(raw_items) > before:
                response_event.set()

        def on_response(response: Any) -> None:
            if not is_relevant_response_url(response.url):
                return
            if authenticated and response.status in {401, 403, 429}:
                account_response_statuses.append(response.status)
            if proxied and response.status == 429:
                proxy_response_statuses.append(response.status)
            response_tasks.append(asyncio.create_task(collect_search_response(response)))

        page.on("response", on_response)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            if authenticated:
                raise AccountUnavailableError("Parser account proxy failed while loading search.", PROXY_ERROR) from exc
            if proxied:
                raise ProxyAccessError("Proxy failed while loading search.") from exc
            raise RuntimeError("Search page load failed or timed out.") from exc

        try:
            await page.locator('a[href*="/post/"]').first.wait_for(state="attached", timeout=7000)
        except PlaywrightTimeoutError:
            pass

        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except PlaywrightTimeoutError:
            body_text = ""
        lower_body = body_text.lower()
        status = response.status if response else None

        if status in {407, 502, 503, 504} and proxied:
            raise ProxyAccessError("Proxy returned an access error.")
        if status == 429:
            if authenticated:
                raise AccountUnavailableError("Parser account was rate limited.", REAUTH_REQUIRED)
            if proxied:
                raise ProxyAccessError("Proxy was rate limited.")
            raise RuntimeError("Threads rate limit was reached.")
        if status == 403 and proxied and not authenticated:
            raise ProxyAccessError("Proxy access was rejected.")
        if status == 404:
            raise RuntimeError("Threads search page is unavailable.")
        if authenticated:
            path = urlparse(page.url).path.lower()
            auth_markers = (
                "challenge required",
                "confirm it's you",
                "suspicious activity",
                "temporarily blocked",
                "too many requests",
            )
            if any(marker in path for marker in ("/login", "/challenge", "/checkpoint")):
                raise AccountUnavailableError("Parser account authorization is no longer valid.", REAUTH_REQUIRED)
            if any(marker in lower_body for marker in auth_markers):
                raise AccountUnavailableError("Parser account challenge was detected.", REAUTH_REQUIRED)
            if status in {401, 403}:
                raise AccountUnavailableError("Parser account authorization is no longer valid.", REAUTH_REQUIRED)

        await dismiss_cookie_banner(page)
        raw_items.extend(await collect_script_json(page))
        await scroll_search(page, post_limit, raw_items, keywords, response_event)
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        if 429 in account_response_statuses:
            raise AccountUnavailableError("Parser account was rate limited.", REAUTH_REQUIRED)
        if 429 in proxy_response_statuses:
            raise ProxyAccessError("Proxy was rate limited during search.")
        if any(item in {401, 403} for item in account_response_statuses):
            raise AccountUnavailableError("Parser account authorization failed during search.", REAUTH_REQUIRED)
        user_agent = await page.evaluate("navigator.userAgent")
        actual_mode, actual_type = search_mode_from_url(page.url)
        return raw_items, user_agent if isinstance(user_agent, str) else "", actual_mode, actual_type
    except (AccountUnavailableError, ProxyAccessError):
        raise
    except PlaywrightError as exc:
        if authenticated:
            raise AccountUnavailableError("Parser account context failed during search.", REAUTH_REQUIRED) from exc
        raise RuntimeError("Search page parsing failed.") from exc
    finally:
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        if page:
            try:
                await page.close()
            except PlaywrightError:
                pass
