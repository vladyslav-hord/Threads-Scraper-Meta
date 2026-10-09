from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

BASE_URL = "https://www.threads.com"
POST_HINT_KEYS = frozenset({"id","pk","text","caption","permalink","media_url","image_versions2","video_versions","carousel_media","timestamp","taken_at","code","shortcode"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
POST_URL_RE = re.compile(r"https?://(?:www\.)?threads\.(?:com|net)/@[^/\s\"']+/post/[^?\s\"']+")
AVATAR_PATH_RE = re.compile(r"/t51\.\d+-19/")
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


def normalize_media_entry(item: dict[str, Any]) -> dict[str, str] | None:
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if is_ignored_media_url(url):
        return None
    media_type = item.get("type")
    if not isinstance(media_type, str) or not media_type:
        media_type = media_type_from_url("", url) or "unknown"
    local_path = item.get("local_path")
    return {
        "type": media_type,
        "url": url,
        "local_path": local_path if isinstance(local_path, str) else "",
    }


def media_key(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    source_url = query.get("url", [""])[0]
    if source_url:
        parsed = urlparse(unquote(source_url))
    filename = Path(unquote(parsed.path)).name.lower()
    return filename or f"{parsed.netloc.lower()}{parsed.path}"


def is_ignored_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return (
        bool(AVATAR_PATH_RE.search(path))
        or "/static/images/spotify_branding/" in path
        or "/static/images/apple_podcasts_branding/" in path
    )


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
    if is_ignored_media_url(url):
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
            if lower_key in {"user", "owner", "author", "viewer"} or any(
                marker in lower_key for marker in ("avatar", "profile_pic", "profile_image")
            ):
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
            if not existing.get("local_path") and item.get("local_path"):
                existing["local_path"] = item["local_path"]
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


def filter_posts_by_keywords(
    posts: list[dict[str, Any]],
    keywords: list[str] | None,
) -> list[dict[str, Any]]:
    if not keywords:
        return posts
    normalized_keywords = tuple(" ".join(keyword.casefold().split()) for keyword in keywords if keyword.strip())
    if not normalized_keywords:
        return posts
    return [
        post
        for post in posts
        if isinstance(post.get("text"), str)
        and any(keyword in " ".join(post["text"].casefold().split()) for keyword in normalized_keywords)
    ]


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
