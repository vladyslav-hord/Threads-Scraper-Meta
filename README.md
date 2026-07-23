## Requirements

- Python 3.11+
- Chromium for Playwright

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python main.py username
```

```bash
python main.py username --scrolls 10
```

```bash
python main.py username --scrolls 10 --proxy socks5://user:pass@host:port
```

Multiple profiles via `users.json`:

```json
{
  "users": ["werunitback", "zuck"]
}
```

```bash
python main.py --users-file users.json --scrolls 10
```

```bash
python main.py --users-file users.json --scrolls 10 --proxy socks5://user:pass@host:port
```

Random HTTP proxy rotation from `proxies.txt`:

```text
host:port:user:password
http://user:password@host:port
```

```bash
python main.py --users-file users.json --scrolls 10 --proxies-file proxies.txt --accounts-per-proxy 3 --workers 3
```

`--accounts-per-proxy` controls the group size. `--workers` controls how many proxy groups run in parallel.

Create local config files from the included examples before batch parsing:

```powershell
Copy-Item users.example.json users.json
Copy-Item proxies.example.txt proxies.txt
```

Result in `output/username/posts.json`, media in `output/username/media/`.
