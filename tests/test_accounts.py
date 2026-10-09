import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError

from threads_parser.accounts import (
    AccountConfig,
    AccountError,
    AccountRuntime,
    TargetJob,
    assign_targets_fair,
    local_session_status,
    load_accounts,
    AccountUnavailableError,
    create_account_context,
    PROXY_ERROR,
    REAUTH_REQUIRED,
    session_path,
)


class AccountConfigTests(unittest.TestCase):
    def write_config(self, root: Path, accounts: list[dict]) -> Path:
        path = root / "accounts.json"
        path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
        return path

    def test_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory),
                [{
                    "id": "acc_01",
                    "username": "parser.user",
                    "proxy": "http://proxy.invalid:8080",
                    "enabled": True,
                    "max_profiles_per_run": 2,
                }],
            )
            account = load_accounts(path)[0]
            self.assertEqual(account.id, "acc_01")
            self.assertEqual(account.max_profiles_per_run, 2)
            self.assertNotIn("proxy.invalid", repr(account))

    def test_duplicate_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = {"id": "same", "username": "parser", "proxy": "http://proxy.invalid:8080"}
            second = {"id": "SAME", "username": "parser", "proxy": "http://proxy.invalid:8081"}
            path = self.write_config(Path(directory), [first, second])
            with self.assertRaises(AccountError):
                load_accounts(path)

    def test_threads_password_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory),
                [{
                    "id": "acc_01",
                    "username": "parser",
                    "proxy": "http://proxy.invalid:8080",
                    "password": "not-allowed",
                }],
            )
            with self.assertRaises(AccountError):
                load_accounts(path)

    def test_socks_proxy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                Path(directory),
                [{
                    "id": "acc_01",
                    "username": "parser",
                    "proxy": "socks5://proxy.invalid:1080",
                }],
            )
            with self.assertRaises(AccountError):
                load_accounts(path)

    def test_session_path_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AccountError):
                session_path("../outside", Path(directory))
            safe = session_path("acc_01", Path(directory))
            self.assertEqual(safe.parent, Path(directory).resolve())

    def test_session_without_auth_cookie_requires_reauth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = load_accounts(
                self.write_config(
                    Path(directory),
                    [{
                        "id": "acc_01",
                        "username": "parser",
                        "proxy": "http://proxy.invalid:8080",
                    }],
                )
            )[0]
            session_path(account.id, Path(directory)).write_text('{"cookies": []}', encoding="utf-8")

            self.assertEqual(local_session_status(account, Path(directory)), REAUTH_REQUIRED)


class AccountContextIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_uses_fixed_proxy_and_state_and_never_retries_direct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = AccountConfig("acc_01", "parser", "http://user:pass@proxy.invalid:8080")
            session_path(account.id, root).write_text(
                json.dumps({"cookies": [{"name": "sessionid", "value": "opaque", "expires": -1}]}),
                encoding="utf-8",
            )
            browser = type("Browser", (), {})()
            browser.new_context = AsyncMock(side_effect=PlaywrightError("proxy context failed"))

            with self.assertRaises(AccountUnavailableError) as raised:
                await create_account_context(browser, account, root)

        self.assertEqual(raised.exception.status, PROXY_ERROR)
        browser.new_context.assert_awaited_once()
        options = browser.new_context.await_args.kwargs
        self.assertEqual(options["proxy"], {
            "server": "http://proxy.invalid:8080",
            "username": "user",
            "password": "pass",
        })
        self.assertEqual(options["storage_state"], session_path(account.id, root))


class AssignmentTests(unittest.TestCase):
    def test_cli_run_limit_does_not_exceed_account_limit(self) -> None:
        account = AccountConfig("acc_01", "one", "http://proxy.invalid:8001", max_profiles_per_run=5)

        runtime = AccountRuntime(account, run_limit=2)
        runtime.record_attempt()
        runtime.record_attempt()

        self.assertEqual(runtime.remaining_capacity, 0)
        self.assertEqual(AccountRuntime(account, run_limit=10).remaining_capacity, 5)

    def test_assignment_is_fair_and_retry_is_limited(self) -> None:
        runtimes = [
            AccountRuntime(AccountConfig("acc_01", "one", "http://proxy.invalid:8001", max_profiles_per_run=3)),
            AccountRuntime(AccountConfig("acc_02", "two", "http://proxy.invalid:8002", max_profiles_per_run=3)),
        ]
        jobs = [TargetJob(f"target_{index}") for index in range(5)]
        grouped, unassigned = assign_targets_fair(jobs, runtimes)

        self.assertFalse(unassigned)
        self.assertEqual([job.username for job in grouped[0][1]], ["target_0", "target_2", "target_4"])
        self.assertEqual([job.username for job in grouped[1][1]], ["target_1", "target_3"])

        job = TargetJob("retry_target")
        job.record_attempt("acc_01")
        self.assertTrue(job.can_retry)
        retry_groups, retry_unassigned = assign_targets_fair([job], runtimes)
        self.assertFalse(retry_unassigned)
        self.assertEqual(retry_groups[0][0].config.id, "acc_02")

        job.record_attempt("acc_02")
        self.assertFalse(job.can_retry)
        exhausted_groups, exhausted = assign_targets_fair([job], runtimes)
        self.assertFalse(exhausted_groups)
        self.assertEqual(exhausted, [job])

    def test_target_job_rejects_retry_on_same_account(self) -> None:
        job = TargetJob("synthetic_target")
        job.record_attempt("acc_01")

        with self.assertRaises(AccountError):
            job.record_attempt("acc_01")


if __name__ == "__main__":
    unittest.main()
