from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BLOCKED_ACCOUNTS_FILE = Path("blocked_accounts.json")
BLOCKED_PROXIES_FILE = Path("blocked_proxies.json")


def _load_entries(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    entries = data.get(key) if isinstance(data, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _write_entries(path: Path, key: str, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps({key: entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def blocked_account_ids(path: Path = BLOCKED_ACCOUNTS_FILE) -> set[str]:
    return {
        entry["id"].lower()
        for entry in _load_entries(path, "accounts")
        if isinstance(entry.get("id"), str)
    }


def blocked_proxies(path: Path = BLOCKED_PROXIES_FILE) -> set[str]:
    return {
        entry["proxy"]
        for entry in _load_entries(path, "proxies")
        if isinstance(entry.get("proxy"), str)
    }


def _quarantine(path: Path, key: str, field: str, value: str, reason: str) -> None:
    entries = _load_entries(path, key)
    entries = [entry for entry in entries if entry.get(field) != value]
    entries.append(
        {
            field: value,
            "reason": reason,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_entries(path, key, entries)


def _restore(path: Path, key: str, field: str, value: str) -> None:
    entries = _load_entries(path, key)
    remaining = [entry for entry in entries if entry.get(field) != value]
    if len(remaining) != len(entries):
        _write_entries(path, key, remaining)


def quarantine_account(account_id: str, reason: str, path: Path = BLOCKED_ACCOUNTS_FILE) -> None:
    _quarantine(path, "accounts", "id", account_id, reason)


def quarantine_proxy(proxy: str, reason: str, path: Path = BLOCKED_PROXIES_FILE) -> None:
    _quarantine(path, "proxies", "proxy", proxy, reason)


def restore_account(account_id: str, path: Path = BLOCKED_ACCOUNTS_FILE) -> None:
    _restore(path, "accounts", "id", account_id)


def restore_proxy(proxy: str, path: Path = BLOCKED_PROXIES_FILE) -> None:
    _restore(path, "proxies", "proxy", proxy)
