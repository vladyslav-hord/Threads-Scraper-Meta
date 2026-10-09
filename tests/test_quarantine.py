import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from threads_parser.quarantine import (
    blocked_account_ids,
    blocked_proxies,
    quarantine_account,
    quarantine_proxy,
    restore_account,
    restore_proxy,
)


class QuarantineTests(unittest.TestCase):
    def test_corrupt_existing_state_fails_closed_without_overwriting(self) -> None:
        malformed_values = [
            "{",
            "[]",
            '{"accounts": {}}',
            '{"accounts": ["bad"]}',
            '{"accounts": [{}]}',
            '{"accounts": [{"id": 1}]}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked_accounts.json"
            for contents in malformed_values:
                path.write_text(contents, encoding="utf-8")
                for operation in (
                    lambda: blocked_account_ids(path),
                    lambda: quarantine_account("acc_01", "reauth_required", path),
                    lambda: restore_account("acc_01", path),
                ):
                    with self.subTest(contents=contents, operation=operation):
                        with self.assertRaises(RuntimeError):
                            operation()
                        self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_unreadable_existing_quarantine_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked_accounts.json"
            path.write_text('{"accounts": []}', encoding="utf-8")
            with patch("threads_parser.quarantine.Path.read_text", side_effect=OSError("denied")):
                with self.assertRaises(RuntimeError):
                    blocked_account_ids(path)

    def test_entries_can_be_quarantined_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accounts_path = root / "blocked_accounts.json"
            proxies_path = root / "blocked_proxies.json"
            proxy = "http://user:password@proxy.invalid:8080"

            quarantine_account("acc_01", "reauth_required", accounts_path)
            quarantine_proxy(proxy, "proxy_error", proxies_path)

            self.assertEqual(blocked_account_ids(accounts_path), {"acc_01"})
            self.assertEqual(blocked_proxies(proxies_path), {proxy})
            self.assertFalse(list(root.glob(".*.tmp")))

            restore_account("acc_01", accounts_path)
            restore_proxy(proxy, proxies_path)
            self.assertFalse(blocked_account_ids(accounts_path))
            self.assertFalse(blocked_proxies(proxies_path))


if __name__ == "__main__":
    unittest.main()
