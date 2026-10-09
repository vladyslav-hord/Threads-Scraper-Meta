import tempfile
import unittest
from pathlib import Path

from threads_parser.quarantine import (
    blocked_account_ids,
    blocked_proxies,
    quarantine_account,
    quarantine_proxy,
    restore_account,
    restore_proxy,
)


class QuarantineTests(unittest.TestCase):
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
