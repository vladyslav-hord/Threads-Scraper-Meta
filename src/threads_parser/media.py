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

class RetryableMediaError(RuntimeError):
    pass

MEDIA_DOWNLOAD_ATTEMPTS=3
RETRYABLE_MEDIA_STATUSES=frozenset({408,425,429,500,502,503,504})
from .parser import *
from .search import safe_filename
from .accounts import AccountUnavailableError
from .traffic import *
def extension_for(url: str, media_type: str) -> str:
    suffix = Path(urlparse(url).path.lower()).suffix
    if suffix in IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS:
        return suffix
    if media_type == "image":
        return ".jpg"
    if media_type == "video":
        return ".mp4"
    return ".bin"


def _existing_media_available(media: dict[str, Any], output_dir: Path) -> bool:
    saved_path = Path(media.get("local_path") or "")
    if not saved_path.name:
        return False
    resolved_path = saved_path.resolve()
    if not resolved_path.is_relative_to(output_dir.resolve()) or not resolved_path.is_file():
        return False
    media["local_path"] = saved_path.as_posix()
    return True


async def _write_playwright_media(
    temporary_path: Path,
    media: dict[str, Any],
    user_agent: str | None,
    request_context: Any,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> None:
    options = {"headers": {"User-Agent": user_agent}} if user_agent else {}
    response = await request_context.get(media["url"], timeout=30000, **options)
    try:
        response_host = (urlparse(response.url).hostname or "").lower()
        threads_host = response_host == "threads.com" or response_host.endswith(".threads.com")
        if response.status in {401, 403} and threads_host:
            raise AccountUnavailableError(
                "Parser account media authorization failed.",
                REAUTH_REQUIRED,
            )
        if response.status == 429 and threads_host:
            raise AccountUnavailableError(
                "Parser account media rate limit was reached.",
                REAUTH_REQUIRED,
            )
        if response.status in RETRYABLE_MEDIA_STATUSES:
            raise RetryableMediaError(f"HTTP {response.status}")
        if "text/html" in response.headers.get("content-type", "").lower():
            raise RuntimeError("Media URL returned HTML.")
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        body = await response.body()
        if traffic_monitor is not None and account_id is not None:
            traffic_monitor.record_api_response(account_id, response.url, response.status, response.headers, len(body))
        if not body:
            raise RetryableMediaError("Empty media response.")
        temporary_path.write_bytes(body)
    finally:
        await response.dispose()


async def _write_httpx_media(
    temporary_path: Path,
    media: dict[str, Any],
    client: httpx.AsyncClient | None,
) -> None:
    if client is None:
        raise RuntimeError("Media client is unavailable.")
    async with client.stream("GET", media["url"]) as response:
        if response.status_code in RETRYABLE_MEDIA_STATUSES:
            raise RetryableMediaError(f"HTTP {response.status_code}")
        response.raise_for_status()
        if "text/html" in response.headers.get("content-type", "").lower():
            raise RuntimeError("Media URL returned HTML.")
        with temporary_path.open("wb") as file:
            async for chunk in response.aiter_bytes():
                if chunk:
                    file.write(chunk)
        if not temporary_path.stat().st_size:
            raise RetryableMediaError("Empty media response.")


async def _download_media_file(
    post_name: str,
    index: int,
    media: dict[str, Any],
    output_dir: Path,
    media_dir: Path,
    semaphore: asyncio.Semaphore,
    user_agent: str | None,
    request_context: Any | None,
    client: httpx.AsyncClient | None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> tuple[bool, bool]:
    extension = extension_for(media["url"], media["type"])
    local_path = media_dir / f"{post_name}_{index:02d}{extension}"
    temporary_path = local_path.with_suffix(f"{local_path.suffix}.part")
    media["local_path"] = local_path.as_posix()

    if local_path.exists():
        local_path.unlink(missing_ok=True)
    temporary_path.unlink(missing_ok=True)

    last_error: BaseException | None = None
    attempts_used = 0
    for attempt in range(1, MEDIA_DOWNLOAD_ATTEMPTS + 1):
        attempts_used = attempt
        try:
            async with semaphore:
                temporary_path.unlink(missing_ok=True)
                if request_context is not None:
                    await _write_playwright_media(
                        temporary_path,
                        media,
                        user_agent,
                        request_context,
                        traffic_monitor,
                        account_id,
                    )
                else:
                    await _write_httpx_media(temporary_path, media, client)
                temporary_path.replace(local_path)
            return True, True
        except AccountUnavailableError:
            raise
        except (PlaywrightError, httpx.TimeoutException, httpx.TransportError, RetryableMediaError, OSError) as exc:
            last_error = exc
            temporary_path.unlink(missing_ok=True)
            if attempt < MEDIA_DOWNLOAD_ATTEMPTS:
                delay = 0.75 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.75)
                await asyncio.sleep(delay)
                continue
        except (httpx.HTTPStatusError, RuntimeError) as exc:
            last_error = exc
            temporary_path.unlink(missing_ok=True)
        break

    error_name = type(last_error).__name__ if last_error else "DownloadError"
    print(
        f"Media download failed for {post_name}_{index:02d} "
        f"after {attempts_used} attempts: {error_name}"
    )
    media["local_path"] = ""
    return False, False


async def download_media(
    posts: list[dict[str, Any]],
    output_dir: Path,
    proxy: str | None,
    user_agent: str | None = None,
    request_context: Any | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    account_id: str | None = None,
) -> tuple[int, int]:
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(30.0, connect=10.0)
    client_options: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
    }
    if user_agent:
        client_options["headers"] = {"User-Agent": user_agent}
    if proxy:
        client_options["proxy"] = proxy

    semaphore = asyncio.Semaphore(5)
    media_entries: list[tuple[str, int, dict[str, Any]]] = []
    existing_available = 0
    for post in posts:
        raw_media = post.get("media") if isinstance(post.get("media"), list) else []
        normalized_media: list[dict[str, str]] = []
        for media in raw_media:
            if not isinstance(media, dict):
                continue
            normalized = normalize_media_entry(media)
            if normalized is not None:
                normalized_media.append(normalized)
        post["media"] = normalized_media
        post_name = safe_filename(post.get("id") or post.get("permalink"))
        for index, media in enumerate(post["media"], start=1):
            if _existing_media_available(media, output_dir):
                existing_available += 1
                continue
            media_entries.append((post_name, index, media))

    if not media_entries:
        return existing_available, 0

    def media_downloads(client: httpx.AsyncClient | None) -> list[Any]:
        downloads = []
        for post_name, index, media in media_entries:
            downloads.append(
                _download_media_file(
                    post_name,
                    index,
                    media,
                    output_dir,
                    media_dir,
                    semaphore,
                    user_agent,
                    request_context,
                    client,
                    traffic_monitor,
                    account_id,
                )
            )
        return downloads

    if request_context is not None:
        results = await asyncio.gather(*media_downloads(None), return_exceptions=True)
    else:
        async with httpx.AsyncClient(**client_options) as active_client:
            results = await asyncio.gather(*media_downloads(active_client), return_exceptions=True)

    for result in results:
        if isinstance(result, AccountUnavailableError):
            raise result
        if isinstance(result, BaseException):
            raise RuntimeError("Media download failed.") from result
    values = [result for result in results if isinstance(result, tuple)]
    return existing_available + sum(available for available, _ in values), sum(downloaded for _, downloaded in values)


def prune_unreferenced_media(output_dir: Path, posts: list[dict[str, Any]]) -> int:
    media_dir = (output_dir / "media").resolve()
    if not media_dir.is_dir():
        return 0

    referenced: set[Path] = set()
    for post in posts:
        for media in post.get("media", []):
            local_path = media.get("local_path") if isinstance(media, dict) else None
            if not isinstance(local_path, str) or not local_path:
                continue
            path = Path(local_path).resolve()
            if path.is_relative_to(media_dir):
                referenced.add(path)

    removed = 0
    for path in media_dir.iterdir():
        if path.is_file() and path.resolve() not in referenced:
            path.unlink()
            removed += 1
    return removed
