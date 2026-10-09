# Meta Threads Scraper

Python scraper for **Meta Threads (threads.com)** with profile crawling, search, media downloads, incremental updates, proxy rotation, and authenticated parser sessions.

Built with **Python, Playwright, and HTTPX**.

## Features

- Public Threads profile scraping
- Keyword/tag search with TOP and RECENT results
- Batch crawling and parallel workers
- `--max-posts` and `--all-posts`
- Keyword and phrase filtering
- Incremental updates and deduplication
- Image/video downloads with retries
- HTTP/HTTPS proxy rotation and quarantine
- Isolated authenticated parser accounts
- Persistent Playwright sessions
- Per-account limits and cross-account retries
- Traffic monitoring
- Automated tests on Python 3.11 and 3.13

## Installation

Requires Python 3.11+ and Chromium.

```bash
git clone https://github.com/vladyslav-hord/meta-threads-scraper.git
cd meta-threads-scraper

python -m venv .venv
python -m pip install -e .
playwright install chromium
```

## Usage

Scrape a profile:

```bash
threads-parser example_user --max-posts 20
```

Full crawl:

```bash
threads-parser example_user --all-posts
```

Batch:

```bash
threads-parser --users-file users.json --max-posts 20
```

Filter posts:

```bash
threads-parser example_user --keywords python "machine learning"
```

Incremental update:

```bash
threads-parser example_user --incremental
```

### Search

```bash
threads-parser \
  --search "machine learning" \
  --search-mode keyword \
  --search-type recent
```

Supported combinations:

- keyword / tag
- top / recent

## Proxies

Single proxy:

```bash
threads-parser example_user \
  --proxy http://user:password@host:port
```

Proxy rotation:

```bash
threads-parser \
  --users-file users.json \
  --proxies-file proxies.txt \
  --profiles-per-proxy 3 \
  --workers 3
```

Failed proxies can be quarantined locally. A configured proxy does not silently fall back to a direct connection.

## Authenticated Parser Accounts

Parser accounts use isolated Playwright sessions and a fixed proxy per account.

```bash
threads-parser \
  --accounts-file accounts.json \
  --login-account parser_01
```

Check sessions:

```bash
threads-parser \
  --accounts-file accounts.json \
  --check-accounts
```

Run:

```bash
threads-parser \
  --users-file users.json \
  --accounts-file accounts.json \
  --workers 3 \
  --incremental
```

If an account becomes unavailable, a retryable target can be reassigned to another healthy account.

Passwords are not stored in `accounts.json`. Sessions, credentials, proxy lists, output, and quarantine state are excluded from Git.

## Architecture

```text
src/threads_parser/
├── cli.py
├── runner.py
├── browser.py
├── search.py
├── parser.py
├── media.py
├── accounts.py
├── proxies.py
├── quarantine.py
├── traffic.py
├── storage.py
└── errors.py
```

The browser layer collects data from Threads, while parsing, media handling, storage, proxy/account management, and orchestration remain separate.

## Development

```bash
python -m pip install -e ".[dev]"
python -m compileall -q src tests
python -m pytest -q
```

CI runs the test suite on Python 3.11 and 3.13.

## Scope

The project is intended for public Threads content and operator-supplied authenticated sessions.

It does not bypass private profiles, CAPTCHA, authentication restrictions, account challenges, or platform access controls.

Threads is an external platform and changes to its internal responses or page structure may require parser updates.

## License

MIT
