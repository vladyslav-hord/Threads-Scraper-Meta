# Threads Parser v2

Concurrent Threads public-data parser with profile/search crawling, incremental updates, filtering, resilient media downloads, proxy support and isolated authenticated parser-account sessions.

## Features

- Single and batch public-profile parsing; keyword/tag search in top or recent order.
- Post limits or full-feed crawling, phrase filters, deduplication, and incremental JSON/media updates.
- Concurrent workers, anonymous HTTP/HTTPS proxies, local quarantine, and traffic reports.
- Optional isolated Playwright contexts per operator-supplied parser account, persistent sessions, fixed proxies, TOTP helper, account checks, and cross-account retries.
- Blocks image, media, and font requests while scanning; media downloads retry with backoff and atomic `.part` files.

## Architecture

`cli` contains argparse validation and the command entry point; `runner` orchestrates profile, search, proxy, and account workflows; `parser` extracts and normalizes posts; `browser` configures Playwright and collects profiles; `search` handles search modes and pagination; `media` downloads media; `proxies` and `accounts` manage network/account inputs; `errors` holds shared workflow exceptions; `quarantine` persists health state; `traffic` reports bytes; `storage` handles saved posts.

## Installation

Python 3.11+ and Chromium are required.

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e '.[dev]'
.venv/Scripts/playwright.exe install chromium
```

Use the platform-equivalent executable path on non-Windows systems.

## Quick start

```bash
python -m threads_parser example_user --max-posts 20
threads-parser example_user --max-posts 20
```

## Profile examples

```bash
python -m threads_parser example_user --max-posts 20
python -m threads_parser --users-file users.json --all-posts --workers 3
python -m threads_parser example_user --keywords python "machine learning"
```

`--max-posts` counts posts after filtering. `--all-posts` scrolls until the feed is exhausted.

## Search examples

```bash
python -m threads_parser --search "beach volleyball" --search-mode keyword --search-type recent
python -m threads_parser --search volleyball --search-mode tag --search-type top
```

## Incremental

```bash
python -m threads_parser example_user --incremental
```

Saved posts remain in place, existing media is reused, and stale unreferenced media is pruned.

## Anonymous proxies

Provide `--proxy http://host:port` or a `--proxies-file proxies.txt` with one HTTP/HTTPS proxy per line. Credentialed proxy URLs are accepted; keep local lists private. Proxy groups run concurrently with `--profiles-per-proxy` and `--workers`.

## Parser accounts

Copy `accounts.example.json` to ignored `accounts.json`, set each account's fixed HTTP/HTTPS proxy, then authenticate interactively:

```bash
python -m threads_parser --accounts-file accounts.json --login-account parser_01
python -m threads_parser --accounts-file accounts.json --check-accounts
python -m threads_parser --users-file users.json --accounts-file accounts.json --workers 3
```

Account sessions are isolated and stored under ignored `sessions/`. `--profiles-per-account` sets a run limit; failed targets can be retried on a different healthy account, never the same account. `otp_code.py` generates a TOTP code interactively.

## Security/privacy

This tool collects public content and can use operator-supplied authenticated parser sessions. Accounts require fixed proxies; proxy failure never falls back to a direct connection. Session and quarantine files may contain authentication or proxy data and are excluded from Git. Never add passwords to `accounts.json`. The tool does not bypass CAPTCHA, restrictions, access controls, or private profiles; follow applicable laws and platform terms.

## Development/testing

```bash
uv pip install --python .venv/Scripts/python.exe -e '.[dev]'
.venv/Scripts/python.exe -m compileall -q src tests
.venv/Scripts/python.exe -m pytest -q
```
