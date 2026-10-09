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
FULL_CRAWL_MAX_SCROLLS=1000
BLOCKED_SCAN_RESOURCE_TYPES=frozenset({"image","media","font"})
SCAN_MEDIA_HOST_MARKERS=("cdninstagram.com","fbcdn.net","giphy.com","tenor.co")
from .parser import *
from .search import *
def is_blocked_scan_request(request: Any) -> bool:
    if request.resource_type in BLOCKED_SCAN_RESOURCE_TYPES:
        return True
    host = (urlparse(request.url).hostname or "").lower()
    return request.resource_type == "fetch" and any(marker in host for marker in SCAN_MEDIA_HOST_MARKERS)


async def configure_scan_context(context: Any) -> None:
    async def handle_route(route: Any, request: Any) -> None:
        if is_blocked_scan_request(request):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handle_route)


def is_relevant_response_url(url: str) -> bool:
    lower = url.lower()
    return "threads." in lower and any(
        marker in lower for marker in ("graphql", "query", "api", "ajax", "post", "media", "thread")
    )


async def collect_response_json(response: Any, raw_items: list[Any]) -> None:
    if not is_relevant_response_url(response.url):
        return

    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type and "graphql" not in response.url.lower():
        return

    try:
        data = await response.json()
    except (PlaywrightError, json.JSONDecodeError, ValueError):
        return

    if isinstance(data, (dict, list)):
        raw_items.append(data)


async def collect_script_json(page: Any) -> list[Any]:
    scripts = await page.evaluate("() => Array.from(document.scripts, s => s.textContent || '')")
    items: list[Any] = []
    for text in scripts:
        items.extend(parse_script_json(text))
    return items


async def dismiss_cookie_banner(page: Any) -> None:
    for label in ("Decline optional cookies", "Allow all cookies"):
        try:
            await page.get_by_text(label, exact=True).click(timeout=1200)
            await page.wait_for_timeout(500)
            return
        except PlaywrightError:
            continue


async def collect_dom_posts(page: Any, username: str) -> list[dict[str, Any]]:
    rows = await page.evaluate(
        """
        (username) => {
          const postPart = `/@${username}/post/`;
          const normalizeHref = (href) => {
            try {
              const url = new URL(href, location.href);
              url.search = "";
              return url.href.replace(/\\/media\\/?$/, "");
            } catch {
              return "";
            }
          };
          const mediaUrl = (url) => typeof url === "string" && /^https?:\\/\\//.test(url);
          const profileImage = (url) => {
            try {
              return /[/]t51[.][0-9]+-19[/]/.test(new URL(url).pathname);
            } catch {
              return false;
            }
          };
          const anchors = Array.from(document.querySelectorAll(`a[href*="${postPart}"]`));
          const seen = new Set();

          return anchors.map((anchor) => {
            const permalink = normalizeHref(anchor.href);
            if (!permalink || !permalink.includes(postPart) || seen.has(permalink)) {
              return null;
            }
            seen.add(permalink);

            let root = anchor.closest("article,[role='article']");
            if (!root) {
              root = anchor;
              for (let i = 0; i < 6 && root.parentElement; i += 1) {
                root = root.parentElement;
                const links = root.querySelectorAll(`a[href*="${postPart}"]`).length;
                if ((root.innerText || "").length > 40 && links <= 3) {
                  break;
                }
              }
            }

            const images = Array.from(root.querySelectorAll("img"))
              .map((img) => ({
                type: "image",
                url: img.currentSrc || img.src,
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0,
              }))
              .filter((item) => mediaUrl(item.url) && !profileImage(item.url) && item.width >= 120 && item.height >= 120);

            const videos = Array.from(root.querySelectorAll("video")).flatMap((video) => {
              const urls = [
                video.currentSrc,
                video.src,
                ...Array.from(video.querySelectorAll("source")).map((source) => source.src),
              ];
              return urls.filter(mediaUrl).map((url) => ({ type: "video", url }));
            });

            const timestampNode = root.querySelector("time");
            return {
              id: permalink.split("/").filter(Boolean).pop() || null,
              username,
              text: (root.innerText || "").trim(),
              timestamp: timestampNode ? timestampNode.getAttribute("datetime") : null,
              permalink,
              media: [...images, ...videos].map((item) => ({
                type: item.type,
                url: item.url,
                local_path: "",
              })),
            };
          }).filter(Boolean);
        }
        """,
        username,
    )
    return rows if isinstance(rows, list) else []


async def scroll_profile(
    page: Any,
    username: str,
    post_limit: int | None,
    raw_items: list[Any],
    dom_posts: list[dict[str, Any]],
    known_permalinks: set[str] | None = None,
    keywords: list[str] | None = None,
) -> None:
    post_part = f"/@{username}/post/"
    stalled = 0
    stall_limit = 3 if post_limit is not None else 4
    await page.mouse.move(640, 700)

    for _ in range(FULL_CRAWL_MAX_SCROLLS):
        collected_posts = merge_posts(
            extract_posts(raw_items, username),
            list(dom_posts),
            prefer_incoming_text=False,
        )
        if post_limit is not None and len(filter_posts_by_keywords(collected_posts, keywords)) >= post_limit:
            break
        before = await page.locator(f'a[href*="{post_part}"]').evaluate_all(
            "links => [...new Set(links.map(link => new URL(link.href).pathname.replace(/\\/media\\/?$/, '').replace(/\\/$/, '')))]"
        )
        await page.mouse.wheel(0, random.randint(1200, 1800))
        try:
            await page.wait_for_function(
                r"""({ postPart, before }) =>
                    [...document.querySelectorAll(`a[href*="${postPart}"]`)]
                      .map(link => new URL(link.href).pathname.replace(/\/media\/?$/, "").replace(/\/$/, ""))
                      .some(path => !before.includes(path))""",
                arg={"postPart": post_part, "before": before},
                timeout=3500,
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(random.randint(350, 650))
        dom_posts.extend(await collect_dom_posts(page, username))

        after = await page.locator(f'a[href*="{post_part}"]').evaluate_all(
            "links => [...new Set(links.map(link => new URL(link.href).pathname.replace(/\\/media\\/?$/, '').replace(/\\/$/, '')))]"
        )
        new_links = set(after) - set(before)
        if known_permalinks and new_links and new_links.issubset(known_permalinks):
            break
        stalled = 0 if new_links else stalled + 1
        if stalled >= stall_limit:
            break


async def load_public_profile(
    username: str,
    post_limit: int | None,
    proxy: str | None,
    known_permalinks: set[str] | None = None,
    keywords: list[str] | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> tuple[list[Any], list[dict[str, Any]], str]:
    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, proxy)
        try:
            return await load_public_profile_in_browser(
                browser,
                username,
                post_limit,
                known_permalinks,
                proxied=proxy is not None,
                keywords=keywords,
                traffic_monitor=traffic_monitor,
                account_id=account_id,
            )
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass


async def launch_browser(playwright: Any, proxy: str | None) -> Any:
    launch_options: dict[str, Any] = {"headless": True}
    proxy_config = playwright_proxy(proxy) if proxy else None
    if proxy_config:
        launch_options["proxy"] = proxy_config
    try:
        return await playwright.chromium.launch(**launch_options)
    except PlaywrightError as exc:
        raise RuntimeError("Browser launch failed.") from exc


async def load_public_profile_in_browser(
    browser: Any,
    username: str,
    post_limit: int | None,
    known_permalinks: set[str] | None = None,
    proxied: bool = False,
    keywords: list[str] | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> tuple[list[Any], list[dict[str, Any]], str]:
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    try:
        await configure_scan_context(context)
        if traffic_monitor is not None and account_id is not None:
            traffic_monitor.attach_context(account_id, context)
        return await load_public_profile_in_context(
            context,
            username,
            post_limit,
            known_permalinks,
            proxied=proxied,
            keywords=keywords,
            traffic_monitor=traffic_monitor,
            account_id=account_id,
        )
    finally:
        try:
            await context.close()
        except PlaywrightError:
            pass


async def load_public_profile_in_context(
    context: Any,
    username: str,
    post_limit: int | None,
    known_permalinks: set[str] | None = None,
    authenticated: bool = False,
    proxied: bool = False,
    keywords: list[str] | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> tuple[list[Any], list[dict[str, Any]], str]:
    raw_items: list[Any] = []
    dom_posts: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task[None]] = []
    account_response_statuses: list[int] = []
    proxy_response_statuses: list[int] = []
    url = f"{BASE_URL}/@{username}"
    page = None

    try:
        try:
            page = await context.new_page()
        except PlaywrightError as exc:
            if authenticated:
                raise AccountUnavailableError("Parser account context is unavailable.", REAUTH_REQUIRED) from exc
            raise RuntimeError("Browser context is unavailable.") from exc

        def on_response(response: Any) -> None:
            if not is_relevant_response_url(response.url):
                return
            if authenticated and response.status in {401, 403, 429}:
                account_response_statuses.append(response.status)
            if proxied and response.status == 429:
                proxy_response_statuses.append(response.status)
            response_tasks.append(asyncio.create_task(collect_response_json(response, raw_items)))

        page.on("response", on_response)

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            if authenticated:
                raise AccountUnavailableError("Parser account proxy failed while loading a target.", PROXY_ERROR) from exc
            if proxied:
                raise ProxyAccessError("Proxy failed while loading a target.") from exc
            raise RuntimeError("Page load failed or timed out.") from exc

        try:
            await page.locator(f'a[href*="/@{username}/post/"]').first.wait_for(state="attached", timeout=5000)
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
        unavailable_text = "page isn't available" in lower_body or "content isn't available" in lower_body
        if status == 404 or (status not in {401, 403} and unavailable_text):
            raise TargetProfileError("Profile not found.", retryable=False)
        if "private" in lower_body and "profile" in lower_body:
            raise TargetProfileError("Profile is private.", retryable=False)
        if authenticated and status in {401, 403}:
            raise AccountUnavailableError("Parser account authorization is no longer valid.", REAUTH_REQUIRED)

        await dismiss_cookie_banner(page)
        raw_items.extend(await collect_script_json(page))
        dom_posts.extend(await collect_dom_posts(page, username))
        await scroll_profile(page, username, post_limit, raw_items, dom_posts, known_permalinks, keywords)
        dom_posts.extend(await collect_dom_posts(page, username))
        if 429 in account_response_statuses:
            raise AccountUnavailableError("Parser account was rate limited.", REAUTH_REQUIRED)
        if 429 in proxy_response_statuses:
            raise ProxyAccessError("Proxy was rate limited during parsing.")
        if any(status in {401, 403} for status in account_response_statuses):
            raise AccountUnavailableError("Parser account authorization failed during parsing.", REAUTH_REQUIRED)
        user_agent = await page.evaluate("navigator.userAgent")
        return raw_items, dom_posts, user_agent if isinstance(user_agent, str) else ""
    except (AccountUnavailableError, ProxyAccessError, TargetProfileError):
        raise
    except PlaywrightError as exc:
        if authenticated:
            raise AccountUnavailableError("Parser account context failed while parsing a target.", REAUTH_REQUIRED) from exc
        raise RuntimeError("Page parsing failed.") from exc
    finally:
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        if page:
            try:
                await page.close()
            except PlaywrightError:
                pass
