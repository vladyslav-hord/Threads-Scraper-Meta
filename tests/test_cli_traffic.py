import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from threads_parser import cli, search


def test_single_anonymous_profile_writes_requested_traffic_report() -> None:
    posts = [{
        "id": "post",
        "username": "target",
        "text": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "permalink": "https://www.threads.com/@target/post/post",
        "media": [],
    }]

    async def load_profile(*args, **kwargs):
        monitor = args[-2] if len(args) >= 6 else kwargs.get("traffic_monitor")
        if monitor is not None:
            monitor.record_api_response("anonymous", "https://www.threads.com/api", 200, "GET", {}, 9)
        return [], [], "ua"

    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        report = Path(directory) / "traffic.json"
        try:
            with (
                patch("sys.argv", ["threads-parser", "target", "--traffic-report", str(report)]),
                patch("threads_parser.cli.load_public_profile", load_profile),
                patch("threads_parser.cli.extract_posts", return_value=posts),
                patch("threads_parser.cli.download_media", AsyncMock(return_value=(0, 0))),
            ):
                cli.main()
            data = json.loads(report.read_text(encoding="utf-8"))
        finally:
            os.chdir(old_cwd)

    assert data["accounts"]["anonymous"]["requests"] == 1
    assert data["accounts"]["anonymous"]["request_bytes"] > 0
    assert data["accounts"]["anonymous"]["response_bytes"] == 9


def test_anonymous_search_writes_requested_traffic_report() -> None:
    posts = [{
        "id": "post",
        "username": "poster",
        "text": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "permalink": "https://www.threads.com/@poster/post/post",
        "media": [],
    }]

    async def load_search(*args, **kwargs):
        monitor = args[-2]
        monitor.record_api_response("anonymous", "https://www.threads.com/search", 200, "GET", {}, 11)
        return [{"search": "payload"}], "ua", "tag", "top"

    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        report = Path(directory) / "search-traffic.json"
        try:
            with (
                patch("sys.argv", ["threads-parser", "--search", "cats", "--traffic-report", str(report)]),
                patch("threads_parser.cli.load_search_results", load_search),
                patch("threads_parser.cli.search_result_connections", return_value=[{}]),
                patch("threads_parser.cli.extract_search_posts", return_value=posts),
                patch("threads_parser.cli.download_media", AsyncMock(return_value=(0, 0))),
            ):
                cli.main()
            data = json.loads(report.read_text(encoding="utf-8"))
        finally:
            os.chdir(old_cwd)

    assert data["accounts"]["anonymous"]["requests"] == 1
    assert data["accounts"]["anonymous"]["response_bytes"] == 11


def test_anonymous_proxy_batch_writes_requested_traffic_report() -> None:
    posts = [{
        "id": "post",
        "username": "target",
        "text": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "permalink": "https://www.threads.com/@target/post/post",
        "media": [],
    }]

    class Playwright:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            pass

    class Browser:
        async def close(self):
            pass

    async def load_profile(*args, **kwargs):
        monitor = kwargs["traffic_monitor"]
        monitor.record_api_response("anonymous", "https://cdn.example/photo.jpg", 200, "GET", {}, 13)
        return [], [], "ua"

    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        report = Path(directory) / "batch-traffic.json"
        try:
            with (
                patch("threads_parser.cli.async_playwright", Playwright),
                patch("threads_parser.cli.launch_browser", AsyncMock(return_value=Browser())),
                patch("threads_parser.cli.load_public_profile_in_browser", load_profile),
                patch("threads_parser.cli.extract_posts", return_value=posts),
                patch("threads_parser.cli.download_media", AsyncMock(return_value=(0, 0))),
            ):
                failures = asyncio.run(
                    cli.run_batch(["target"], 1, "http://proxy.invalid:8080", traffic_report_path=report)
                )
            data = json.loads(report.read_text(encoding="utf-8"))
        finally:
            os.chdir(old_cwd)

    assert failures == []
    assert data["accounts"]["anonymous"]["requests"] == 1
    assert data["accounts"]["anonymous"]["response_bytes"] == 13


def test_anonymous_profile_does_not_create_report_when_option_is_omitted() -> None:
    posts = [{
        "id": "post",
        "username": "target",
        "text": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "permalink": "https://www.threads.com/@target/post/post",
        "media": [],
    }]

    async def load_profile(*_args, **_kwargs):
        return [], [], "ua"

    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        try:
            with (
                patch("sys.argv", ["threads-parser", "target"]),
                patch("threads_parser.cli.load_public_profile", load_profile),
                patch("threads_parser.cli.extract_posts", return_value=posts),
                patch("threads_parser.cli.download_media", AsyncMock(return_value=(0, 0))),
            ):
                cli.main()
            assert not (Path(directory) / "runs").exists()
        finally:
            os.chdir(old_cwd)


def test_anonymous_profile_writes_traffic_report_after_runtime_failure(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        report = Path(directory) / "failed-profile-traffic.json"
        try:
            with (
                patch("sys.argv", ["threads-parser", "target", "--traffic-report", str(report)]),
                patch("threads_parser.cli.run", AsyncMock(side_effect=RuntimeError("profile failed"))),
            ):
                try:
                    cli.main()
                except SystemExit as exc:
                    assert exc.code == 1
                else:
                    raise AssertionError("CLI should exit unsuccessfully")

            data = json.loads(report.read_text(encoding="utf-8"))
            assert data["accounts"] == {}
            assert "Error: profile failed" in capsys.readouterr().out
        finally:
            os.chdir(old_cwd)


def test_anonymous_search_writes_traffic_report_after_proxy_failure(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        old_cwd = Path.cwd()
        os.chdir(directory)
        report = Path(directory) / "failed-search-traffic.json"
        try:
            with (
                patch("sys.argv", ["threads-parser", "--search", "cats", "--proxy", "http://proxy.invalid:8080", "--traffic-report", str(report)]),
                patch("threads_parser.cli.run_search", AsyncMock(side_effect=cli.ProxyAccessError("blocked"))),
                patch("threads_parser.cli.quarantine_proxy") as quarantine_proxy,
            ):
                try:
                    cli.main()
                except SystemExit as exc:
                    assert exc.code == 1
                else:
                    raise AssertionError("CLI should exit unsuccessfully")

            assert report.exists()
            assert "accounts" in json.loads(report.read_text(encoding="utf-8"))
            quarantine_proxy.assert_called_once_with("http://proxy.invalid:8080", cli.PROXY_ERROR)
            assert "Error: Proxy was quarantined after an access failure." in capsys.readouterr().out
        finally:
            os.chdir(old_cwd)


def test_search_browser_context_is_attached_to_traffic_monitor() -> None:
    class Context:
        def __init__(self):
            self.handlers = {}

        async def route(self, *_args):
            pass

        def on(self, event, handler):
            self.handlers[event] = handler

        async def close(self):
            pass

    class Browser:
        def __init__(self):
            self.context = Context()

        async def new_context(self, **_kwargs):
            return self.context

    monitor = cli.TrafficMonitor()
    request = type("Request", (), {
        "url": "https://www.threads.com/search/?q=hello",
        "resource_type": "document",
        "method": "GET",
        "headers": {},
        "post_data_buffer": None,
    })()

    async def load_context(context, *_args, **kwargs):
        context.handlers["request"](request)
        assert kwargs["traffic_monitor"] is monitor
        return [], "ua", "tag", "top"

    with patch("threads_parser.search.load_search_results_in_context", load_context):
        result = asyncio.run(
            search.load_search_results_in_browser(
                Browser(), "hello", 1, traffic_monitor=monitor, account_id="anonymous"
            )
        )

    assert result == ([], "ua", "tag", "top")
    assert monitor.accounts["anonymous"].request_count == 1
