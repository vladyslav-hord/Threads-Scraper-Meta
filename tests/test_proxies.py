import tempfile
import unittest
from pathlib import Path

from threads_parser.cli import load_proxies


class ProxyFileTests(unittest.TestCase):
    def test_proxy_formats_are_loaded_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "host.invalid:8080:user:pass",
                        "http://user:pass@host.invalid:8080",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_proxies(path),
                [
                    "http://user:pass@host.invalid:8080",
                ],
            )

    def test_proxy_urls_with_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            path.write_text("http://proxy.invalid:8080/path\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                load_proxies(path)

    def test_socks_proxy_urls_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            path.write_text("socks5://proxy.invalid:1080\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                load_proxies(path)


if __name__ == "__main__":
    unittest.main()
