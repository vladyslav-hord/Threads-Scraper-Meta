import tempfile
import unittest
from pathlib import Path

from threads_parser.accounts import AccountError
from login_accounts import load_credentials


class CredentialsFileTests(unittest.TestCase):
    def test_tab_separated_credentials_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.txt"
            path.write_text("parser.user\tpassword\tJBSW Y3DP\n", encoding="utf-8")
            credentials = load_credentials(path)
            self.assertEqual(credentials["parser.user"], ("password", "JBSWY3DP"))

    def test_duplicate_usernames_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.txt"
            path.write_text("Parser\tone\tSECRET\nparser\ttwo\tSECRET\n", encoding="utf-8")
            with self.assertRaises(AccountError):
                load_credentials(path)


if __name__ == "__main__":
    unittest.main()
