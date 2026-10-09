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
@dataclass
class TrafficBucket:
    request_count: int = 0
    response_count: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    unknown_response_bytes: int = 0
    statuses: dict[str, int] = field(default_factory=dict)


class TrafficMonitor:
    def __init__(self) -> None:
        self.accounts: dict[str, TrafficBucket] = {}
        self.domains: dict[str, TrafficBucket] = {}
        self.resource_types: dict[str, TrafficBucket] = {}
        self.urls: dict[str, TrafficBucket] = {}

    def attach_context(self, account_id: str, context: Any) -> None:
        context.on("request", lambda request: self.record_request(account_id, request))
        context.on("response", lambda response: self.record_response(account_id, response))

    def record_request(self, account_id: str, request: Any) -> None:
        url = getattr(request, "url", "")
        resource_type = getattr(request, "resource_type", "unknown") or "unknown"
        size = estimate_request_bytes(request)
        for bucket in self._buckets(account_id, url, resource_type):
            bucket.request_count += 1
            bucket.request_bytes += size

    def record_response(self, account_id: str, response: Any) -> None:
        url = getattr(response, "url", "")
        request = getattr(response, "request", None)
        resource_type = getattr(request, "resource_type", "unknown") if request else "unknown"
        status = str(getattr(response, "status", "unknown"))
        size = content_length(response.headers)
        unknown = 1 if size is None else 0
        for bucket in self._buckets(account_id, url, resource_type or "unknown"):
            bucket.response_count += 1
            bucket.response_bytes += size or 0
            bucket.unknown_response_bytes += unknown
            bucket.statuses[status] = bucket.statuses.get(status, 0) + 1

    def record_api_response(
        self,
        account_id: str,
        url: str,
        status: int,
        method: str,
        request_headers: dict[str, str],
        body_size: int,
        request_body: bytes | None = None,
    ) -> None:
        request = type("APIRequest", (), {
            "url": url,
            "method": method,
            "headers": request_headers,
            "post_data_buffer": request_body,
        })()
        request_size = estimate_request_bytes(request)
        for bucket in self._buckets(account_id, url, "media_api"):
            bucket.request_count += 1
            bucket.request_bytes += request_size
            bucket.response_count += 1
            bucket.response_bytes += body_size
            bucket.statuses[str(status)] = bucket.statuses.get(str(status), 0) + 1

    def report(self) -> dict[str, Any]:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "accounts": serialize_buckets(self.accounts),
            "domains": serialize_buckets(self.domains),
            "resource_types": serialize_buckets(self.resource_types),
            "top_urls": serialize_buckets(
                dict(sorted(self.urls.items(), key=lambda item: item[1].response_bytes, reverse=True)[:50])
            ),
        }

    def print_summary(self, report_path: Path) -> None:
        print()
        print("Proxy traffic:")
        for account_id, bucket in sorted(self.accounts.items()):
            total = bucket.request_bytes + bucket.response_bytes
            unknown = f", unknown responses: {bucket.unknown_response_bytes}" if bucket.unknown_response_bytes else ""
            print(
                f"{account_id}: {format_bytes(total)} "
                f"(up {format_bytes(bucket.request_bytes)}, down {format_bytes(bucket.response_bytes)}{unknown})"
            )
        print(f"Traffic report: {report_path.as_posix()}")

    def _buckets(self, account_id: str, url: str, resource_type: str) -> list[TrafficBucket]:
        domain = urlparse(url).netloc.lower() or "unknown"
        normalized_url = normalize_traffic_url(url)
        return [
            self.accounts.setdefault(account_id, TrafficBucket()),
            self.domains.setdefault(domain, TrafficBucket()),
            self.resource_types.setdefault(resource_type, TrafficBucket()),
            self.urls.setdefault(normalized_url, TrafficBucket()),
        ]


def content_length(headers: dict[str, str]) -> int | None:
    value = headers.get("content-length") or headers.get("Content-Length")
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def estimate_request_bytes(request: Any) -> int:
    url = str(getattr(request, "url", ""))
    method = getattr(request, "method", "GET") or "GET"
    parsed = urlparse(url)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    size = len(f"{method} {target} HTTP/1.1\r\n".encode("utf-8"))
    headers = getattr(request, "headers", {}) or {}
    for key, value in headers.items():
        size += len(f"{key}: {value}\r\n".encode("utf-8"))
    body = getattr(request, "post_data_buffer", None)
    if body:
        size += len(body)
    return size


def normalize_traffic_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def serialize_buckets(values: dict[str, TrafficBucket]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "requests": bucket.request_count,
            "responses": bucket.response_count,
            "request_bytes": bucket.request_bytes,
            "response_bytes": bucket.response_bytes,
            "total_bytes": bucket.request_bytes + bucket.response_bytes,
            "unknown_response_bytes": bucket.unknown_response_bytes,
            "statuses": dict(sorted(bucket.statuses.items())),
        }
        for key, bucket in sorted(values.items())
    }


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def default_traffic_report_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path("runs") / f"proxy_traffic_{stamp}.json"
