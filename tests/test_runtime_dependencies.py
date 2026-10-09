import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from threads_parser import browser, media, search
from threads_parser.accounts import AccountUnavailableError, REAUTH_REQUIRED
from threads_parser.errors import ProxyAccessError


class Locator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def inner_text(self, **_kwargs):
        return ""


class Mouse:
    async def move(self, *_args):
        return None


class SearchPage:
    def __init__(self):
        self.url = "https://www.threads.com/search/?q=cats&serp_type=tags"
        self.mouse = Mouse()

    def on(self, *_args):
        return None

    async def goto(self, *_args, **_kwargs):
        return SimpleNamespace(status=200)

    def locator(self, *_args):
        return Locator()

    def get_by_text(self, *_args, **_kwargs):
        return SimpleNamespace(click=self._click)

    async def _click(self, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args):
        return None

    async def evaluate(self, expression):
        if "document.scripts" in expression:
            return []
        if expression == "navigator.userAgent":
            return "synthetic-agent"
        raise AssertionError(f"Unexpected evaluate call: {expression}")

    async def close(self):
        return None


class Context:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_browser_launch_uses_explicit_proxy_adapter() -> None:
    class Chromium:
        def __init__(self):
            self.options = None

        async def launch(self, **options):
            self.options = options
            return object()

    chromium = Chromium()
    result = asyncio.run(
        browser.launch_browser(
            SimpleNamespace(chromium=chromium),
            "http://user:pass@proxy.invalid:8080",
        )
    )

    assert result is not None
    assert chromium.options["proxy"] == {
        "server": "http://proxy.invalid:8080",
        "username": "user",
        "password": "pass",
    }


def test_real_search_context_path_resolves_browser_helpers() -> None:
    result = asyncio.run(
        search.load_search_results_in_context(
            Context(SearchPage()),
            "cats",
            0,
            search_mode="tag",
            search_type="top",
        )
    )

    assert result == ([], "synthetic-agent", "tag", "top")


def test_authenticated_profile_403_raises_account_error() -> None:
    class Page(SearchPage):
        url = "https://www.threads.com/@target"

        async def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=403)

    with pytest.raises(AccountUnavailableError) as raised:
        asyncio.run(
            browser.load_public_profile_in_context(
                Context(Page()),
                "target",
                1,
                authenticated=True,
            )
        )

    assert raised.value.status == REAUTH_REQUIRED


def test_search_proxy_failure_raises_proxy_error() -> None:
    class Page(SearchPage):
        async def goto(self, *_args, **_kwargs):
            raise PlaywrightError("proxy failed")

    with pytest.raises(ProxyAccessError):
        asyncio.run(
            search.load_search_results_in_context(
                Context(Page()),
                "cats",
                1,
                proxied=True,
            )
        )


@pytest.mark.parametrize("status", [401, 429])
def test_threads_media_auth_failure_raises_account_error(tmp_path: Path, status: int) -> None:
    class Response:
        url = "https://www.threads.com/media/file.jpg"
        headers = {"content-type": "image/jpeg"}
        ok = False

        async def body(self):
            return b""

        async def dispose(self):
            return None

    response = Response()
    response.status = status

    class RequestContext:
        async def get(self, *_args, **_kwargs):
            return response

    with pytest.raises(AccountUnavailableError) as raised:
        asyncio.run(
            media._write_playwright_media(
                tmp_path / "media.jpg.part",
                {"url": response.url},
                "agent",
                RequestContext(),
            )
        )

    assert raised.value.status == REAUTH_REQUIRED
