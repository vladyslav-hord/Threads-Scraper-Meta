import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from threads_parser.media import _write_httpx_media
from threads_parser.traffic import estimate_request_bytes

from threads_parser.cli import (
    PlaywrightError,
    TrafficMonitor,
    collect_media,
    configure_scan_context,
    download_media,
    extract_search_posts,
    filter_posts_by_keywords,
    is_blocked_scan_request,
    is_ignored_media_url,
    load_saved_posts,
    parse_args,
    prune_unreferenced_media,
    run,
    run_search,
    search_has_next_page,
    search_mode_from_url,
    search_output_dir,
    search_url,
    scroll_profile,
    scroll_search,
)


class MediaFilterTests(unittest.TestCase):
    def test_keywords_match_post_text_case_insensitively(self) -> None:
        posts = [
            {"id": "one", "text": "Python and Machine Learning"},
            {"id": "two", "text": "Travel photography"},
            {"id": "three", "text": None},
        ]

        matched = filter_posts_by_keywords(posts, ["PYTHON", "data science"])

        self.assertEqual([post["id"] for post in matched], ["one"])

    def test_keywords_cli_accepts_words_and_phrases(self) -> None:
        with patch("sys.argv", ["main.py", "target", "--keywords", "Python", "machine learning"]):
            args = parse_args()

        self.assertEqual(args.keywords, ["Python", "machine learning"])
        self.assertEqual(args.max_posts, 20)

    def test_max_posts_cli_sets_target_and_all_posts_disables_it(self) -> None:
        with patch("sys.argv", ["main.py", "target", "--max-posts", "7"]):
            limited = parse_args()
        with patch("sys.argv", ["main.py", "target", "--all-posts"]):
            unlimited = parse_args()

        self.assertEqual(limited.max_posts, 7)
        self.assertIsNone(unlimited.max_posts)

    def test_search_cli_is_a_target(self) -> None:
        with patch("sys.argv", ["main.py", "--search", "  beach   volleyball  ", "--max-posts", "50"]):
            args = parse_args()

        self.assertEqual(args.search, "beach volleyball")
        self.assertIsNone(args.username)
        self.assertEqual(args.max_posts, 50)
        self.assertEqual(args.search_mode, "tag")
        self.assertEqual(args.search_type, "top")

    def test_search_cli_accepts_mode_and_type(self) -> None:
        with patch(
            "sys.argv",
            ["main.py", "--search", "volleyball", "--search-mode", "keyword", "--search-type", "recent"],
        ):
            args = parse_args()

        self.assertEqual(args.search_mode, "keyword")
        self.assertEqual(args.search_type, "recent")

    def test_search_options_require_search_target(self) -> None:
        with (
            patch("sys.argv", ["main.py", "target", "--search-mode", "tag"]),
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            parse_args()

    def test_search_options_are_rejected_for_account_management(self) -> None:
        with (
            patch(
                "sys.argv",
                ["main.py", "--accounts-file", "accounts.json", "--check-accounts", "--search-type", "recent"],
            ),
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            parse_args()

    def test_search_cli_rejects_a_profile_target(self) -> None:
        with (
            patch("sys.argv", ["main.py", "target", "--search", "volleyball"]),
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            parse_args()

    def test_search_extraction_keeps_only_primary_results(self) -> None:
        def post(identifier, username, text):
            return {
                "id": identifier,
                "code": identifier,
                "user": {"username": username},
                "caption": {"text": text},
                "taken_at": 1_700_000_000,
            }

        raw_items = [{
            "data": {
                "searchResults": {
                    "edges": [
                        {"node": {"thread": {"thread_items": [
                            {"post": post("primary_one", "one", "Volleyball one")},
                            {"post": post("context_one", "context", "Quoted context")},
                        ]}}},
                        {"node": {"thread": {"thread_items": [
                            {"post": post("primary_two", "two", "Volleyball two")},
                        ]}}},
                    ],
                    "page_info": {"has_next_page": True},
                }
            }
        }]

        posts = extract_search_posts(raw_items)

        self.assertEqual([post["id"] for post in posts], ["primary_one", "primary_two"])
        self.assertEqual([post["username"] for post in posts], ["one", "two"])
        self.assertTrue(search_has_next_page(raw_items))
        raw_items.append({"data": {"searchResults": {"edges": [], "page_info": {"has_next_page": False}}}})
        self.assertFalse(search_has_next_page(raw_items))

    def test_search_output_paths_do_not_collide_after_sanitizing(self) -> None:
        self.assertEqual(search_output_dir("volleyball"), Path("output/search/tag/top/volleyball"))
        self.assertEqual(
            search_output_dir("volleyball", "keyword", "recent"),
            Path("output/search/keyword/recent/volleyball"),
        )
        self.assertNotEqual(search_output_dir("C++"), search_output_dir("C#"))

    def test_search_urls_cover_all_modes_and_types(self) -> None:
        self.assertEqual(search_url("beach volleyball", "keyword", "top"), "https://www.threads.com/search/?q=beach%20volleyball")
        self.assertEqual(
            search_url("beach volleyball", "keyword", "recent"),
            "https://www.threads.com/search/?q=beach%20volleyball&filter=recent",
        )
        self.assertEqual(
            search_url("beach volleyball", "tag", "top"),
            "https://www.threads.com/search/?q=beach%20volleyball&serp_type=tags",
        )
        self.assertEqual(
            search_url("beach volleyball", "tag", "recent"),
            "https://www.threads.com/search/?q=beach%20volleyball&serp_type=tags&filter=recent",
        )

    def test_search_mode_is_detected_after_redirect(self) -> None:
        self.assertEqual(
            search_mode_from_url("https://www.threads.com/search/?q=volleyball&serp_type=tags&filter=recent"),
            ("tag", "recent"),
        )

    def test_avatar_cdn_variants_are_ignored(self) -> None:
        self.assertTrue(is_ignored_media_url("https://cdn.example/v/t51.2885-19/avatar.jpg"))
        self.assertTrue(is_ignored_media_url("https://cdn.example/v/t51.82787-19/avatar.jpg"))
        self.assertFalse(is_ignored_media_url("https://cdn.example/v/t51.2885-15/post.jpg"))

    def test_apple_podcasts_branding_is_ignored(self) -> None:
        url = "https://www.instagram.com/static/images/apple_podcasts_branding/apple_podcasts_full_logo_white.png"
        item = {"image_url": url, "media_url": "https://cdn.example/post.jpg"}

        self.assertTrue(is_ignored_media_url(url))
        self.assertEqual([media["url"] for media in collect_media(item)], ["https://cdn.example/post.jpg"])

    def test_profile_image_keys_are_ignored(self) -> None:
        item = {
            "profile_pic_url": "https://cdn.example/avatar.jpg",
            "media_url": "https://cdn.example/post.jpg",
        }
        self.assertEqual([media["url"] for media in collect_media(item)], ["https://cdn.example/post.jpg"])

    def test_incremental_cleanup_removes_only_unreferenced_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "target"
            media_dir = output_dir / "media"
            media_dir.mkdir(parents=True)
            kept = media_dir / "kept.jpg"
            stale = media_dir / "avatar.jpg"
            kept.write_bytes(b"post")
            stale.write_bytes(b"avatar")
            posts = [{"media": [{"local_path": kept.as_posix()}]}]

            self.assertEqual(prune_unreferenced_media(output_dir, posts), 1)
            self.assertTrue(kept.exists())
            self.assertFalse(stale.exists())

    def test_saved_media_entries_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            posts_path = Path(directory) / "posts.json"
            posts_path.write_text(
                """
                [{
                    "id": "post",
                    "media": [
                        {"url": "https://cdn.example/photo.jpg"},
                        {"url": "socks5://cdn.example/photo.jpg"},
                        {"url": "https://cdn.example/v/t51.2885-19/avatar.jpg"},
                        {"type": "image"}
                    ]
                }]
                """,
                encoding="utf-8",
            )

            posts = load_saved_posts(posts_path)

        self.assertEqual(posts[0]["media"], [{
            "type": "image",
            "url": "https://cdn.example/photo.jpg",
            "local_path": "",
        }])


class ScanNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_context_blocks_heavy_assets_only(self) -> None:
        class Context:
            handler = None

            async def route(self, pattern, handler):
                self.pattern = pattern
                self.handler = handler

        class Route:
            aborted = False
            continued = False

            async def abort(self):
                self.aborted = True

            async def continue_(self):
                self.continued = True

        class Request:
            def __init__(self, resource_type, url="https://www.threads.com/api/graphql"):
                self.resource_type = resource_type
                self.url = url

        context = Context()
        await configure_scan_context(context)

        self.assertEqual(context.pattern, "**/*")
        for resource_type in ("image", "media", "font"):
            route = Route()
            await context.handler(route, Request(resource_type))
            self.assertTrue(route.aborted)
            self.assertFalse(route.continued)

        for resource_type in ("document", "script", "stylesheet", "xhr"):
            route = Route()
            await context.handler(route, Request(resource_type))
            self.assertFalse(route.aborted)
            self.assertTrue(route.continued)

        cdn_fetch = Route()
        await context.handler(
            cdn_fetch,
            Request("fetch", "https://scontent.example.cdninstagram.com/video.mp4"),
        )
        self.assertTrue(cdn_fetch.aborted)

        api_fetch = Request("fetch", "https://www.threads.com/api/graphql")
        self.assertFalse(is_blocked_scan_request(api_fetch))


class MediaRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_accepts_no_results(self) -> None:
        raw_items = [{"data": {"searchResults": {"edges": [], "page_info": {"has_next_page": False}}}}]
        download = AsyncMock(return_value=(0, 0))

        with tempfile.TemporaryDirectory() as directory:
            previous_cwd = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch(
                        "threads_parser.cli.load_search_results",
                        AsyncMock(return_value=(raw_items, "user-agent", "tag", "top")),
                    ),
                    patch("threads_parser.cli.download_media", download),
                ):
                    await run_search("volleyball", 20, None)
                saved = json.loads(Path("output/search/tag/top/volleyball/posts.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(saved, [])
        self.assertEqual(download.await_args_list[0].args[0], [])

    async def test_run_accepts_profile_with_no_keyword_matches(self) -> None:
        posts = [{
            "id": "skipped",
            "username": "target",
            "text": "Travel update",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "permalink": "https://example.com/skipped",
            "media": [{"type": "image", "url": "https://cdn.example/skipped.jpg", "local_path": ""}],
        }]
        download = AsyncMock(return_value=(0, 0))

        with tempfile.TemporaryDirectory() as directory:
            previous_cwd = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch("threads_parser.cli.load_public_profile", AsyncMock(return_value=([], [], "user-agent"))),
                    patch("threads_parser.cli.extract_posts", return_value=posts),
                    patch("threads_parser.cli.download_media", download),
                ):
                    await run("target", 1, None, keywords=["python"])
                saved = json.loads(Path("output/target/posts.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(download.await_args_list[0].args[0], [])
        self.assertEqual(saved, [])

    async def test_run_downloads_and_saves_only_keyword_matches(self) -> None:
        posts = [
            {
                "id": "matching",
                "username": "target",
                "text": "Python release",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "permalink": "https://example.com/matching",
                "media": [{"type": "image", "url": "https://cdn.example/matching.jpg", "local_path": ""}],
            },
            {
                "id": "skipped",
                "username": "target",
                "text": "Travel update",
                "timestamp": "2026-01-02T00:00:00+00:00",
                "permalink": "https://example.com/skipped",
                "media": [{"type": "image", "url": "https://cdn.example/skipped.jpg", "local_path": ""}],
            },
        ]
        load_profile = AsyncMock(return_value=([], [], "user-agent"))
        download = AsyncMock(return_value=(1, 1))

        with tempfile.TemporaryDirectory() as directory:
            previous_cwd = Path.cwd()
            os.chdir(directory)
            try:
                with (
                    patch("threads_parser.cli.load_public_profile", load_profile),
                    patch("threads_parser.cli.extract_posts", return_value=posts),
                    patch("threads_parser.cli.download_media", download),
                ):
                    await run("target", 1, None, keywords=["python"])
                saved = json.loads(Path("output/target/posts.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous_cwd)

        downloaded_posts = download.await_args_list[0].args[0]
        self.assertEqual([post["id"] for post in downloaded_posts], ["matching"])
        self.assertEqual([post["id"] for post in saved], ["matching"])

    async def test_existing_local_media_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "target"
            media_dir = output_dir / "media"
            media_dir.mkdir(parents=True)
            existing = media_dir / "existing.jpg"
            existing.write_bytes(b"existing")
            posts = [{
                "id": "post",
                "media": [{
                    "url": "https://cdn.example/existing.jpg",
                    "local_path": existing.as_posix(),
                }],
            }]

            self.assertEqual(await download_media(posts, output_dir, None), (1, 0))
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertEqual(posts[0]["media"][0]["type"], "image")

    async def test_missing_media_list_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "target"
            posts = [{"id": "post"}]

            self.assertEqual(await download_media(posts, output_dir, None), (0, 0))
            self.assertEqual(posts[0]["media"], [])

    async def test_transient_errors_are_retried_and_written_atomically(self) -> None:
        class Response:
            status = 200
            url = "https://cdn.example/media.jpg"
            headers = {"content-type": "image/jpeg"}
            ok = True

            async def body(self) -> bytes:
                return b"image"

            async def dispose(self) -> None:
                pass

        class FlakyRequest:
            calls = 0

            async def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise PlaywrightError("timeout")
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "target"
            posts = [{"id": "post", "media": [{"type": "image", "url": "https://cdn.example/media.jpg", "local_path": ""}]}]
            request = FlakyRequest()
            with patch("threads_parser.media.asyncio.sleep", return_value=None), patch("threads_parser.media.random.uniform", return_value=0.0):
                available, downloaded = await download_media(
                    posts,
                    output_dir,
                    None,
                    request_context=request,
                )

            local_path = Path(posts[0]["media"][0]["local_path"])
            self.assertEqual(request.calls, 3)
            self.assertEqual((available, downloaded), (1, 1))
            self.assertEqual(local_path.read_bytes(), b"image")
            self.assertFalse(local_path.with_suffix(".jpg.part").exists())

    async def test_authenticated_media_download_records_traffic(self) -> None:
        class Response:
            status = 200
            url = "https://cdn.example/media.jpg"
            headers = {"content-type": "image/jpeg"}
            ok = True

            async def body(self) -> bytes:
                return b"image"

            async def dispose(self) -> None:
                pass

        class Request:
            async def get(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "target"
            posts = [{"id": "post", "media": [{"type": "image", "url": "https://cdn.example/media.jpg", "local_path": ""}]}]
            monitor = TrafficMonitor()
            available, downloaded = await download_media(
                posts,
                output_dir,
                None,
                request_context=Request(),
                traffic_monitor=monitor,
                account_id="acc_01",
            )

            self.assertEqual((available, downloaded), (1, 1))
            self.assertEqual(monitor.accounts["acc_01"].response_bytes, 5)
            self.assertEqual(monitor.domains["cdn.example"].statuses, {"200": 1})

    async def test_httpx_media_records_actual_request_and_response_traffic(self) -> None:
        class Response:
            status_code = 206
            headers = {"content-type": "image/jpeg"}
            request = httpx.Request("GET", "https://cdn.example/photo.jpg?size=large", headers={"x-test": "request"})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"abc"

        class Client:
            def stream(self, *_args):
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            monitor = TrafficMonitor()
            await _write_httpx_media(
                Path(directory) / "photo.jpg",
                {"url": "https://cdn.example/photo.jpg?size=large", "type": "image"},
                Client(),
                monitor,
                "anonymous",
            )

        bucket = monitor.accounts["anonymous"]
        assert bucket.request_bytes == estimate_request_bytes(Response.request) > 0
        assert bucket.request_count == 1
        assert bucket.response_count == 1
        assert bucket.response_bytes == 3
        assert bucket.statuses == {"206": 1}


class ScrollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_scroll_does_not_stall_before_page_bottom(self) -> None:
        class Mouse:
            wheel_calls = 0

            async def move(self, *_args):
                pass

            async def wheel(self, *_args):
                self.wheel_calls += 1

        class Page:
            mouse = Mouse()

            async def evaluate(self, *_args):
                return {
                    "top": min(self.mouse.wheel_calls * 1800, 9000),
                    "height": 10000,
                    "viewport": 900,
                }

            async def wait_for_timeout(self, *_args):
                pass

        page = Page()
        one = [{"id": "one", "text": "one"}]
        two = one + [{"id": "two", "text": "two"}]

        with (
            patch("threads_parser.search.extract_search_posts", side_effect=lambda _items: two if page.mouse.wheel_calls >= 5 else one),
            patch("threads_parser.search.search_has_next_page", return_value=True),
        ):
            await scroll_search(page, 2, [])

        self.assertEqual(page.mouse.wheel_calls, 5)

    async def test_search_scroll_stops_at_last_page_bottom(self) -> None:
        class Mouse:
            wheel_calls = 0

            async def move(self, *_args):
                pass

            async def wheel(self, *_args):
                self.wheel_calls += 1

        class Page:
            mouse = Mouse()

            async def evaluate(self, *_args):
                return {"top": 9100, "height": 10000, "viewport": 900}

        page = Page()
        with (
            patch("threads_parser.search.extract_search_posts", return_value=[]),
            patch("threads_parser.search.search_has_next_page", return_value=False),
        ):
            await scroll_search(page, None, [])

        self.assertEqual(page.mouse.wheel_calls, 0)

    async def test_scroll_uses_api_posts_for_target_count(self) -> None:
        class Mouse:
            wheel_calls = 0

            async def move(self, *_args):
                pass

            async def wheel(self, *_args):
                self.wheel_calls += 1

        class Page:
            mouse = Mouse()

        api_posts = [
            {"id": "one", "text": "Python one"},
            {"id": "two", "text": "Python two"},
        ]
        page = Page()

        with patch("threads_parser.browser.extract_posts", return_value=api_posts):
            await scroll_profile(page, "target", 2, [{}], [], keywords=["python"])

        self.assertEqual(page.mouse.wheel_calls, 0)

    async def test_scroll_continues_until_keyword_post_target_is_reached(self) -> None:
        class Mouse:
            wheel_calls = 0

            async def move(self, *_args):
                pass

            async def wheel(self, *_args):
                self.wheel_calls += 1

        class Locator:
            calls = 0

            async def evaluate_all(self, *_args):
                self.calls += 1
                states = [[], ["/@target/post/one"], ["/@target/post/one"], ["/@target/post/one", "/@target/post/two"]]
                return states[self.calls - 1]

        class Page:
            mouse = Mouse()
            links = Locator()

            def locator(self, *_args):
                return self.links

            async def wait_for_function(self, *_args, **_kwargs):
                pass

            async def wait_for_timeout(self, *_args):
                pass

        dom_posts: list[dict] = []
        collected = [
            [{"id": "one", "text": "Travel", "permalink": "https://www.threads.com/@target/post/one"}],
            [{"id": "two", "text": "Python", "permalink": "https://www.threads.com/@target/post/two"}],
        ]
        page = Page()

        with patch("threads_parser.browser.collect_dom_posts", AsyncMock(side_effect=collected)):
            await scroll_profile(page, "target", 1, [], dom_posts, keywords=["python"])

        self.assertEqual(page.mouse.wheel_calls, 2)
        self.assertEqual([post["id"] for post in dom_posts], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
