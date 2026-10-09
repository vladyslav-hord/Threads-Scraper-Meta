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

from .accounts import AccountError, validate_proxy, playwright_proxy
def clean_proxy(value: str) -> str:
    proxy = value.strip()
    try:
        return validate_proxy(proxy)
    except AccountError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
