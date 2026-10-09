from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.threads.com"
LOGIN_URL = f"{BASE_URL}/login"
SESSIONS_DIR = Path("sessions")

READY = "ready"
MISSING_SESSION = "missing_session"
REAUTH_REQUIRED = "reauth_required"
PROXY_ERROR = "proxy_error"
DISABLED = "disabled"
MAX_TARGET_ATTEMPTS = 2

ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.]+")
AUTH_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss"})
AUTH_PATH_MARKERS = ("/login", "/challenge", "/checkpoint")
AUTH_TEXT_MARKERS = (
    "challenge required",
    "confirm it's you",
    "suspicious activity",
    "temporarily blocked",
    "too many requests",
)
ACCOUNT_FIELDS = frozenset({"id", "username", "proxy", "enabled", "max_profiles_per_run"})


class AccountError(RuntimeError):
    pass


class AccountUnavailableError(AccountError):
    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AccountConfig:
    id: str
    username: str
    proxy: str = field(repr=False)
    enabled: bool = True
    max_profiles_per_run: int = 20


@dataclass
class AccountRuntime:
    config: AccountConfig
    run_limit: int | None = None
    used: int = 0
    healthy: bool = True
    status: str = READY
    context: Any | None = field(default=None, repr=False)

    @property
    def remaining_capacity(self) -> int:
        limit = self.config.max_profiles_per_run
        if self.run_limit is not None:
            limit = min(limit, self.run_limit)
        return max(0, limit - self.used)

    def record_attempt(self) -> None:
        if self.remaining_capacity < 1:
            raise AccountError(f"Parser account {self.config.id} reached its run limit.")
        self.used += 1

    def disable(self, status: str) -> None:
        self.healthy = False
        self.status = status


@dataclass
class TargetJob:
    username: str
    attempted_accounts: set[str] = field(default_factory=set)

    @property
    def can_retry(self) -> bool:
        return len(self.attempted_accounts) < MAX_TARGET_ATTEMPTS

    def record_attempt(self, account_id: str) -> None:
        if account_id in self.attempted_accounts:
            raise AccountError("A target cannot be retried by the same parser account.")
        self.attempted_accounts.add(account_id)


def validate_account_id(value: Any) -> str:
    if not isinstance(value, str) or not ACCOUNT_ID_RE.fullmatch(value):
        raise AccountError("Account id must contain only letters, digits, underscores or hyphens.")
    return value


def validate_proxy(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AccountError("Account proxy must be a non-empty URL.")

    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AccountError("Account proxy must contain a valid port.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not port:
        raise AccountError("Account proxy must use http or https and include host:port.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise AccountError("Account proxy URL cannot contain a path, query or fragment.")
    return value


def load_accounts(path: Path) -> list[AccountConfig]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise AccountError(f"Cannot read accounts file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AccountError(f"Invalid JSON in accounts file: {path}") from exc

    values = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(values, list) or not values:
        raise AccountError('Accounts file must contain a non-empty {"accounts": [...]} list.')

    accounts: list[AccountConfig] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            raise AccountError(f"Account entry {index} must be an object.")
        if any("password" in key.casefold() for key in value):
            raise AccountError(f"Account entry {index} must not contain a Threads password.")
        if set(value) - ACCOUNT_FIELDS:
            raise AccountError(f"Account entry {index} contains unsupported fields.")

        try:
            account_id = validate_account_id(value.get("id"))
        except AccountError as exc:
            raise AccountError(f"Invalid account id at entry {index}.") from exc
        normalized_id = account_id.casefold()
        if normalized_id in seen_ids:
            raise AccountError(f"Duplicate account id: {account_id}")

        username = value.get("username")
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise AccountError(f"Invalid Threads username for account {account_id}.")
        try:
            proxy = validate_proxy(value.get("proxy"))
        except AccountError as exc:
            raise AccountError(f"Invalid proxy for account {account_id}.") from exc

        enabled = value.get("enabled", True)
        limit = value.get("max_profiles_per_run", 20)
        if not isinstance(enabled, bool):
            raise AccountError(f"enabled must be boolean for account {account_id}.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise AccountError(f"max_profiles_per_run must be at least 1 for account {account_id}.")

        accounts.append(AccountConfig(account_id, username, proxy, enabled, limit))
        seen_ids.add(normalized_id)
    return accounts


def get_account(accounts: list[AccountConfig], account_id: str) -> AccountConfig:
    safe_id = validate_account_id(account_id)
    for account in accounts:
        if account.id == safe_id:
            return account
    raise AccountError(f"Parser account not found: {safe_id}")


def session_path(account_id: str, sessions_dir: Path = SESSIONS_DIR) -> Path:
    safe_id = validate_account_id(account_id)
    root = sessions_dir.resolve()
    path = (root / f"{safe_id}.json").resolve()
    if path.parent != root:
        raise AccountError("Unsafe session path.")
    return path


def local_session_status(account: AccountConfig, sessions_dir: Path = SESSIONS_DIR) -> str:
    if not account.enabled:
        return DISABLED
    path = session_path(account.id, sessions_dir)
    if not path.is_file():
        return MISSING_SESSION
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return REAUTH_REQUIRED
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list):
        return REAUTH_REQUIRED
    return READY if _has_live_session_cookie(cookies) else REAUTH_REQUIRED


def playwright_proxy(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    config = {"server": f"{parsed.scheme}://{netloc}"}
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    return config


async def create_account_context(
    browser: Any,
    account: AccountConfig,
    sessions_dir: Path = SESSIONS_DIR,
) -> Any:
    status = local_session_status(account, sessions_dir)
    if status != READY:
        raise AccountUnavailableError("Parser account session is unavailable.", status)
    try:
        return await browser.new_context(
            proxy=playwright_proxy(account.proxy),
            storage_state=session_path(account.id, sessions_dir),
            viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
    except PlaywrightError as exc:
        raise AccountUnavailableError("Parser account session must be refreshed.", REAUTH_REQUIRED) from exc


def _has_live_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    now = time.time()
    return any(
        cookie.get("name") in AUTH_COOKIE_NAMES
        and bool(cookie.get("value"))
        and (not isinstance(cookie.get("expires"), (int, float)) or cookie["expires"] <= 0 or cookie["expires"] > now)
        for cookie in cookies
    )


async def inspect_account_context(context: Any) -> str:
    page = None
    try:
        page = await context.new_page()
        response = await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)

        status = response.status if response else None
        if status in {407, 502, 503, 504}:
            return PROXY_ERROR
        if status in {401, 403, 429}:
            return REAUTH_REQUIRED

        path = urlparse(page.url).path.lower()
        if any(marker in path for marker in AUTH_PATH_MARKERS):
            return REAUTH_REQUIRED

        try:
            body = (await page.locator("body").inner_text(timeout=5000)).lower()
        except PlaywrightTimeoutError:
            body = ""
        if any(marker in body for marker in AUTH_TEXT_MARKERS):
            return REAUTH_REQUIRED
        if await page.locator('input[type="password"], input[name="username"]').count():
            return REAUTH_REQUIRED

        cookies = await context.cookies()
        return READY if _has_live_session_cookie(cookies) else REAUTH_REQUIRED
    except (PlaywrightTimeoutError, PlaywrightError):
        return PROXY_ERROR
    finally:
        if page:
            try:
                await page.close()
            except PlaywrightError:
                pass


def _write_session_atomic(path: Path, state: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".session.tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(state, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except (OSError, TypeError, ValueError) as exc:
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise AccountError("Cannot save parser account session.") from exc


async def save_account_session(
    context: Any,
    account_id: str,
    sessions_dir: Path = SESSIONS_DIR,
) -> None:
    try:
        state = await context.storage_state(indexed_db=True)
    except PlaywrightError as exc:
        raise AccountError("Parser account session could not be read.") from exc
    _write_session_atomic(session_path(account_id, sessions_dir), state)


async def login_account(
    playwright: Any,
    account: AccountConfig,
    sessions_dir: Path = SESSIONS_DIR,
) -> None:
    try:
        browser = await playwright.chromium.launch(headless=False)
    except PlaywrightError as exc:
        raise AccountError("Headed Chromium launch failed.") from exc

    context = None
    try:
        options: dict[str, Any] = {
            "proxy": playwright_proxy(account.proxy),
            "viewport": {"width": 1280, "height": 900},
        }
        if local_session_status(account, sessions_dir) == READY:
            options["storage_state"] = session_path(account.id, sessions_dir)
        try:
            context = await browser.new_context(**options)
        except PlaywrightError as exc:
            if "storage_state" not in options:
                raise AccountError("Parser account context could not be created.") from exc
            options.pop("storage_state")
            try:
                context = await browser.new_context(**options)
            except PlaywrightError as retry_exc:
                raise AccountError("Parser account context could not be created.") from retry_exc
        try:
            page = await context.new_page()
        except PlaywrightError as exc:
            raise AccountError("Parser account context could not be created.") from exc
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise AccountUnavailableError("Parser account proxy is unavailable.", PROXY_ERROR) from exc

        print(
            f"Parser account {account.id}: sign in as @{account.username}, "
            "then complete 2FA or challenge in the browser."
        )
        try:
            await asyncio.to_thread(input, "Press Enter after Threads login is complete: ")
        except EOFError as exc:
            raise AccountError("Interactive input is required for login.") from exc

        status = await inspect_account_context(context)
        if status != READY:
            raise AccountUnavailableError("Threads login was not confirmed.", status)
        await save_account_session(context, account.id, sessions_dir)
    finally:
        if context:
            try:
                await context.close()
            except PlaywrightError:
                pass
        try:
            await browser.close()
        except PlaywrightError:
            pass


async def check_accounts(
    playwright: Any,
    accounts: list[AccountConfig],
    sessions_dir: Path = SESSIONS_DIR,
) -> dict[str, str]:
    statuses = {account.id: local_session_status(account, sessions_dir) for account in accounts}
    candidates = [account for account in accounts if statuses[account.id] == READY]
    if not candidates:
        return statuses

    try:
        browser = await playwright.chromium.launch(headless=True)
    except PlaywrightError as exc:
        raise AccountError("Chromium launch failed.") from exc

    try:
        for account in candidates:
            context = None
            try:
                context = await create_account_context(browser, account, sessions_dir)
                statuses[account.id] = await inspect_account_context(context)
            except AccountUnavailableError as exc:
                statuses[account.id] = exc.status
            finally:
                if context:
                    try:
                        await context.close()
                    except PlaywrightError:
                        statuses[account.id] = PROXY_ERROR
    finally:
        try:
            await browser.close()
        except PlaywrightError:
            pass
    return statuses


def assign_targets_fair(
    jobs: list[TargetJob],
    runtimes: list[AccountRuntime],
) -> tuple[list[tuple[AccountRuntime, list[TargetJob]]], list[TargetJob]]:
    assignments = {runtime.config.id: [] for runtime in runtimes}
    reserved = {runtime.config.id: 0 for runtime in runtimes}
    unassigned: list[TargetJob] = []

    for job in jobs:
        candidates = [
            (runtime.used + reserved[runtime.config.id], index, runtime)
            for index, runtime in enumerate(runtimes)
            if runtime.config.enabled
            and runtime.healthy
            and runtime.remaining_capacity > reserved[runtime.config.id]
            and runtime.config.id not in job.attempted_accounts
            and job.can_retry
        ]
        if not candidates:
            unassigned.append(job)
            continue
        _, _, selected = min(candidates, key=lambda item: (item[0], item[1]))
        assignments[selected.config.id].append(job)
        reserved[selected.config.id] += 1

    grouped = [(runtime, assignments[runtime.config.id]) for runtime in runtimes if assignments[runtime.config.id]]
    return grouped, unassigned
