from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_URL = "https://www.threads.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
POST_HINT_KEYS = frozenset(
    {
        "id",
        "pk",
        "text",
        "caption",
        "permalink",
        "media_url",
        "image_versions2",
        "video_versions",
        "carousel_media",
        "timestamp",
        "taken_at",
        "code",
        "shortcode",
    }
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
POST_URL_RE = re.compile(r"https?://(?:www\.)?threads\.(?:com|net)/@[^/\s\"']+/post/[^?\s\"']+")


def clean_username(value: str) -> str:
    username = value.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", username):
        raise argparse.ArgumentTypeError("username can contain only letters, digits, dots and underscores")
    return username


def clean_proxy(value: str) -> str:
    proxy = value.strip()
    parsed = urlparse(proxy)
    try:
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("proxy must contain a valid port") from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not port:
        raise argparse.ArgumentTypeError("proxy must look like socks5://host:port")
    return proxy


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def load_proxies(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read proxies file: {path}") from exc

    proxies: list[str] = []
    for line_number, raw_line in enumerate(lines, start=1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue

        if "://" not in value:
            parts = value.split(":")
            if len(parts) == 2:
                value = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                host, port, username, password = parts
                value = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
            else:
                raise RuntimeError(f"Invalid proxy format at line {line_number}.")

        try:
            proxy = clean_proxy(value)
        except argparse.ArgumentTypeError as exc:
            raise RuntimeError(f"Invalid proxy at line {line_number}.") from exc
        if proxy not in proxies:
            proxies.append(proxy)

    if not proxies:
        raise RuntimeError("Proxies file is empty.")
    return proxies


def playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None

    parsed = urlparse(proxy)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    config = {"server": f"{parsed.scheme}://{netloc}"}
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test parser for public Threads profiles.")
    parser.add_argument("username", nargs="?", type=clean_username)
    parser.add_argument("--users-file", type=Path, help="JSON file with a list of usernames")
    parser.add_argument("--scrolls", type=int, default=5)
    parser.add_argument("--proxy", type=clean_proxy, help="Optional proxy URL, e.g. socks5://user:pass@host:port")
    parser.add_argument("--proxies-file", type=Path, help="Proxy list, one proxy per line")
    parser.add_argument("--accounts-per-proxy", type=positive_int, default=3)
    parser.add_argument("--workers", type=positive_int, default=3)
    args = parser.parse_args()
    if bool(args.username) == bool(args.users_file):
        parser.error("provide either username or --users-file")
    if args.proxy and args.proxies_file:
        parser.error("provide either --proxy or --proxies-file")
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


def walk_json(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def parse_script_json(text: str) -> list[Any]:
    text = text.strip()
    if not text or not any(key in text for key in POST_HINT_KEYS):
        return []

    decoder = json.JSONDecoder()
    parsed: list[Any] = []
    starts = [0] if text[:1] in "[{" else [m.start() for m in re.finditer(r"[\[{]", text)]

    for start in starts[:400]:
        try:
            item, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(item, (dict, list)):
            parsed.append(item)
    return parsed


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
    except Exception:
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
        except Exception:
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
              return new URL(url).pathname.includes("/t51.2885-19/");
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
    count: int,
    dom_posts: list[dict[str, Any]],
) -> None:
    post_part = f"/@{username}/post/"
    stalled = 0
    await page.mouse.move(640, 700)

    for _ in range(max(0, count)):
        before = await page.locator(f'a[href*="{post_part}"]').evaluate_all(
            "links => [...new Set(links.map(link => link.href.replace(/\\/media\\/?$/, '')))]"
        )
        await page.mouse.wheel(0, random.randint(1200, 1800))
        try:
            await page.wait_for_function(
                r"""({ postPart, before }) =>
                    [...document.querySelectorAll(`a[href*="${postPart}"]`)]
                      .map(link => link.href.replace(/\/media\/?$/, ""))
                      .some(href => !before.includes(href))""",
                arg={"postPart": post_part, "before": before},
                timeout=3500,
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(random.randint(350, 650))
        dom_posts.extend(await collect_dom_posts(page, username))

        after = await page.locator(f'a[href*="{post_part}"]').evaluate_all(
            "links => [...new Set(links.map(link => link.href.replace(/\\/media\\/?$/, '')))]"
        )
        stalled = 0 if set(after) - set(before) else stalled + 1
        if stalled >= 2:
            break


def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.isdigit():
            return normalize_timestamp(int(value))
        return value
    return None


def normalize_permalink(value: str) -> str | None:
    value = unquote(value.strip())
    if not value:
        return None
    if value.startswith("/@"):
        return f"{BASE_URL}{value}"
    match = POST_URL_RE.search(value)
    if not match:
        return None
    return re.sub(r"/media/?$", "", match.group(0))


def media_type_from_url(key: str, url: str) -> str | None:
    lower_key = key.lower()
    parsed = urlparse(url)
    suffix = Path(parsed.path.lower()).suffix
    host = parsed.netloc.lower()

    if suffix in VIDEO_EXTENSIONS or "video" in lower_key:
        return "video"
    if suffix in IMAGE_EXTENSIONS or any(marker in lower_key for marker in ("image", "photo", "thumbnail", "display")):
        return "image"
    if "media_url" in lower_key and any(marker in host for marker in ("fbcdn", "cdninstagram", "scontent", "threads.")):
        return "unknown"
    return None


def media_key(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    source_url = query.get("url", [""])[0]
    if source_url:
        source = urlparse(unquote(source_url))
        return f"{source.netloc.lower()}{source.path}"
    return f"{parsed.netloc.lower()}{parsed.path}"


def is_profile_image_url(url: str) -> bool:
    return "/t51.2885-19/" in urlparse(url).path


def media_score(item: dict[str, Any]) -> int:
    width = item.get("width") or item.get("original_width") or item.get("w") or 0
    height = item.get("height") or item.get("original_height") or item.get("h") or 0
    try:
        return int(width) * int(height)
    except (TypeError, ValueError):
        return 0


def best_candidate_url(value: Any) -> str | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict)]
    elif isinstance(value, dict):
        raw_candidates = value.get("candidates") or value.get("additional_candidates")
        if isinstance(raw_candidates, list):
            candidates = [item for item in raw_candidates if isinstance(item, dict)]
        elif isinstance(value.get("url"), str):
            candidates = [value]

    best = max(candidates, key=media_score, default=None)
    url = best.get("url") if best else None
    return url if isinstance(url, str) else None


def add_media_url(media: list[dict[str, str]], seen: set[str], url: Any, media_type: str | None) -> None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return
    if is_profile_image_url(url):
        return
    key = media_key(url)
    if key in seen:
        return
    seen.add(key)
    media.append({"type": media_type or "unknown", "url": url, "local_path": ""})


def collect_media(item: dict[str, Any]) -> list[dict[str, str]]:
    media: list[dict[str, str]] = []
    seen: set[str] = set()
    stack: list[tuple[Any, str | None]] = [(item, None)]

    while stack:
        current, forced_type = stack.pop()
        if isinstance(current, list):
            stack.extend((entry, forced_type) for entry in current)
            continue
        if not isinstance(current, dict):
            continue

        for key, value in current.items():
            lower_key = key.lower()
            if lower_key in {"user", "owner", "author", "viewer"}:
                continue
            if lower_key in {"image_versions2", "image_versions"}:
                add_media_url(media, seen, best_candidate_url(value), "image")
                continue
            if lower_key == "video_versions":
                add_media_url(media, seen, best_candidate_url(value), "video")
                continue
            if lower_key in {"carousel_media", "carousel_media_v2"}:
                stack.append((value, None))
                continue
            if isinstance(value, str):
                media_type = forced_type or media_type_from_url(key, value)
                if media_type:
                    add_media_url(media, seen, value, media_type)
            elif isinstance(value, (dict, list)):
                stack.append((value, forced_type))

    return media


def normalize_post(item: dict[str, Any], target_username: str) -> dict[str, Any] | None:
    if not set(item) & POST_HINT_KEYS:
        return None

    username = target_username
    for source in (item, item.get("user"), item.get("owner"), item.get("author")):
        if not isinstance(source, dict):
            continue
        value = first_value(source, ("username", "user_name"))
        if isinstance(value, str) and value.strip():
            username = value.strip().lstrip("@")
            break

    permalink = None
    for value in (item.get(key) for key in ("permalink", "url", "post_url", "share_url")):
        if isinstance(value, str) and (permalink := normalize_permalink(value)):
            break

    code = first_value(item, ("code", "shortcode"))
    shortcode = code.strip() if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,}", code.strip()) else None
    if not permalink and shortcode:
        permalink = f"{BASE_URL}/@{username}/post/{shortcode}"

    identifier = first_value(item, ("id", "pk", "media_id")) or shortcode
    if not identifier and permalink:
        identifier = permalink.rstrip("/").split("/")[-1]
    if not identifier:
        return None

    text = ""
    text_value = first_value(item, ("text", "caption", "caption_text"))
    if isinstance(text_value, dict):
        text_value = first_value(text_value, ("text", "body", "plain_text"))
    if isinstance(text_value, str):
        text = text_value.strip()

    timestamp = None
    for value in (item.get(key) for key in ("timestamp", "taken_at", "taken_at_timestamp", "created_at", "created_time")):
        if timestamp := normalize_timestamp(value):
            break
    if not text and not timestamp and not permalink:
        return None

    return {
        "id": str(identifier),
        "username": username,
        "text": text,
        "timestamp": timestamp,
        "permalink": permalink,
        "media": collect_media(item),
    }


def merge_media(target: list[dict[str, str]], incoming: list[dict[str, str]]) -> None:
    indexed = {media_key(item["url"]): item for item in target if item.get("url")}
    for item in incoming:
        url = item.get("url")
        if not url:
            continue
        key = media_key(url)
        existing = indexed.get(key)
        if existing:
            if existing["type"] == "unknown" and item["type"] != "unknown":
                existing["type"] = item["type"]
            continue
        target.append(item)
        indexed[key] = item


def merge_post(target: dict[str, Any], incoming: dict[str, Any], prefer_incoming_text: bool = True) -> None:
    for key in ("id", "username", "timestamp", "permalink"):
        if not target.get(key) and incoming.get(key):
            target[key] = incoming[key]
    if incoming.get("text") and (
        not target.get("text") or (prefer_incoming_text and len(incoming.get("text") or "") > len(target.get("text") or ""))
    ):
        target["text"] = incoming["text"]
    merge_media(target["media"], incoming["media"])


def post_dedupe_keys(post: dict[str, Any]) -> list[str]:
    keys = [str(key) for key in (post.get("id"), post.get("permalink")) if key]
    text = re.sub(r"\s+", " ", post.get("text") or "").strip().lower()
    username = (post.get("username") or "").lower()
    if len(text) >= 24:
        keys.append(f"text:{username}:{text}")
    return keys


def merge_posts(
    posts: list[dict[str, Any]],
    incoming_posts: list[dict[str, Any]],
    prefer_incoming_text: bool = True,
) -> list[dict[str, Any]]:
    posts_by_key: dict[str, dict[str, Any]] = {}
    for post in posts:
        for key in post_dedupe_keys(post):
            posts_by_key[key] = post

    for post in incoming_posts:
        keys = post_dedupe_keys(post)
        existing = next((posts_by_key[key] for key in keys if key in posts_by_key), None)
        if existing:
            merge_post(existing, post, prefer_incoming_text=prefer_incoming_text)
            for key in keys:
                posts_by_key[key] = existing
            continue
        posts.append(post)
        for key in keys:
            posts_by_key[key] = post

    return posts


def extract_posts(raw_items: list[Any], target_username: str) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    target = target_username.lower()

    for raw in raw_items:
        for item in walk_json(raw):
            post = normalize_post(item, target_username)
            if not post:
                continue

            username = (post["username"] or "").lower()
            permalink = (post["permalink"] or "").lower()
            if username != target and f"/@{target}/post/" not in permalink:
                continue
            posts.append(post)
    return merge_posts([], posts)


def safe_filename(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "post").strip("._")
    return cleaned[:80] or "post"


def extension_for(url: str, media_type: str) -> str:
    suffix = Path(urlparse(url).path.lower()).suffix
    if suffix in IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS:
        return suffix
    if media_type == "image":
        return ".jpg"
    if media_type == "video":
        return ".mp4"
    return ".bin"


async def download_media(posts: list[dict[str, Any]], output_dir: Path, proxy: str | None) -> int:
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(30.0, connect=10.0)
    client_options: dict[str, Any] = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": timeout,
        "follow_redirects": True,
    }
    if proxy:
        client_options["proxy"] = proxy

    semaphore = asyncio.Semaphore(5)

    async with httpx.AsyncClient(**client_options) as client:
        async def save_media(post_name: str, index: int, media: dict[str, Any]) -> bool:
            extension = extension_for(media["url"], media["type"])
            local_path = media_dir / f"{post_name}_{index:02d}{extension}"
            media["local_path"] = local_path.as_posix()

            if local_path.exists():
                return True

            try:
                async with semaphore:
                    async with client.stream("GET", media["url"]) as response:
                        response.raise_for_status()
                        with local_path.open("wb") as file:
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    file.write(chunk)
                return True
            except httpx.HTTPError as exc:
                print(f"Media download failed for {post_name}_{index:02d}: {type(exc).__name__}")
                media["local_path"] = ""
                return False

        downloads = []
        for post in posts:
            post_name = safe_filename(post.get("id") or post.get("permalink"))
            for index, media in enumerate(post["media"], start=1):
                downloads.append(save_media(post_name, index, media))

        results = await asyncio.gather(*downloads)
    return sum(results)


async def load_public_profile(
    username: str,
    scrolls: int,
    proxy: str | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, proxy)
        try:
            return await load_public_profile_in_browser(browser, username, scrolls)
        finally:
            await browser.close()


async def launch_browser(playwright: Any, proxy: str | None) -> Any:
    launch_options: dict[str, Any] = {"headless": True}
    proxy_config = playwright_proxy(proxy)
    if proxy_config:
        launch_options["proxy"] = proxy_config
    try:
        return await playwright.chromium.launch(**launch_options)
    except PlaywrightError as exc:
        raise RuntimeError("Browser launch failed.") from exc


async def load_public_profile_in_browser(
    browser: Any,
    username: str,
    scrolls: int,
) -> tuple[list[Any], list[dict[str, Any]]]:
    raw_items: list[Any] = []
    dom_posts: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task[None]] = []
    url = f"{BASE_URL}/@{username}"
    context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    try:
        def on_response(response: Any) -> None:
            response_tasks.append(asyncio.create_task(collect_response_json(response, raw_items)))

        page.on("response", on_response)

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
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

        if status == 404 or "page isn't available" in lower_body or "content isn't available" in lower_body:
            raise RuntimeError("Profile not found.")
        if "private" in lower_body and "profile" in lower_body:
            raise RuntimeError("Profile is private.")

        await dismiss_cookie_banner(page)
        raw_items.extend(await collect_script_json(page))
        dom_posts.extend(await collect_dom_posts(page, username))
        await scroll_profile(page, username, scrolls, dom_posts)
        dom_posts.extend(await collect_dom_posts(page, username))
        return raw_items, dom_posts
    finally:
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        await context.close()


async def run(username: str, scrolls: int, proxy: str | None, browser: Any | None = None) -> None:
    if browser is None:
        raw_items, dom_posts = await load_public_profile(username, scrolls, proxy)
    else:
        raw_items, dom_posts = await load_public_profile_in_browser(browser, username, scrolls)
    posts = merge_posts(extract_posts(raw_items, username), dom_posts, prefer_incoming_text=False)
    if not posts:
        raise RuntimeError("Posts not found. The profile may be restricted or the proxy may be blocked.")

    output_dir = Path("output") / username
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = await download_media(posts, output_dir, proxy)

    with (output_dir / "posts.json").open("w", encoding="utf-8") as file:
        json.dump(posts, file, ensure_ascii=False, indent=2)

    print(f"Profile: {username}")
    print(f"Posts found: {len(posts)}")
    print(f"Media downloaded: {downloaded}")
    print(f"Output: {output_dir.as_posix()}")


def build_proxy_groups(
    usernames: list[str],
    proxies: list[str] | None,
    accounts_per_proxy: int,
) -> list[tuple[list[str], str | None]]:
    if not proxies:
        return [(usernames, None)]

    groups: list[tuple[list[str], str | None]] = []
    available: list[str] = []
    for start in range(0, len(usernames), accounts_per_proxy):
        if not available:
            available = proxies.copy()
            random.shuffle(available)
        groups.append((usernames[start : start + accounts_per_proxy], available.pop()))
    return groups


async def run_batch(
    usernames: list[str],
    scrolls: int,
    proxy: str | None,
    proxies: list[str] | None = None,
    accounts_per_proxy: int = 3,
    workers: int = 3,
) -> list[str]:
    failed: list[str] = []
    groups = build_proxy_groups(usernames, proxies, accounts_per_proxy)
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
                    for username in group_usernames:
                        try:
                            await run(username, scrolls, active_proxy, browser)
                        except RuntimeError as exc:
                            failed.append(username)
                            print(f"Profile: {username}")
                            print(f"Error: {exc}")
                finally:
                    await browser.close()
                    queue.task_done()

        worker_count = min(workers if proxies else 1, len(groups))
        await asyncio.gather(*(process_groups() for _ in range(worker_count)))

    print()
    print(f"Profiles processed: {len(usernames)}")
    print(f"Profiles failed: {len(failed)}")
    return failed


def main() -> None:
    args = parse_args()
    try:
        proxies = load_proxies(args.proxies_file) if args.proxies_file else None
        if args.username:
            selected_proxy = random.choice(proxies) if proxies else args.proxy
            asyncio.run(run(args.username, args.scrolls, selected_proxy))
            return

        usernames = load_usernames(args.users_file)
        failed = asyncio.run(
            run_batch(
                usernames,
                args.scrolls,
                args.proxy,
                proxies,
                args.accounts_per_proxy,
                args.workers,
            )
        )
        if failed:
            raise SystemExit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
