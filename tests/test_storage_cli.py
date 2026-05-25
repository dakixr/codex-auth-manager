from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_auth_manager import cli, storage
from codex_auth_manager import rpc
from codex_auth_manager.rpc import RateLimit, Snapshot, Window


def auth_data(refresh: str, email: str = "user@example.com") -> dict[str, object]:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": _jwt({"exp": 1_900_000_000, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}),
            "id_token": _jwt({"exp": 1_900_000_000, "email": email}),
            "refresh_token": refresh,
            "account_id": email,
        },
    }


def _jwt(payload: dict[str, object]) -> str:
    import base64

    raw = json.dumps(payload).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{encoded}.sig"


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.codex_home = root / "codex"
        self.codex_home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                storage.VAULT_ENV: str(self.vault),
                storage.CODEX_HOME_ENV: str(self.codex_home),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_switch_and_identify_active(self) -> None:
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))

        self.assertEqual([account.name for account in storage.list_accounts()], ["one", "two"])
        storage.switch_account("one")

        self.assertEqual(storage.identify_active(), "one")
        self.assertEqual(storage.read_auth(storage.active_auth_path())["tokens"]["refresh_token"], "one")

    def test_quota_syncs_rotated_auth_to_saved_and_active(self) -> None:
        storage.write_auth(storage.active_auth_path(), auth_data("old", "one@example.com"))
        storage.write_auth(storage.account_path("one"), auth_data("old", "one@example.com"))
        rotated = auth_data("new", "one@example.com")
        snapshot = Snapshot(
            email="one@example.com",
            plan="plus",
            auth_method="chatgpt",
            default_limit=RateLimit(
                limit_id="codex",
                limit_name=None,
                primary=Window(used_percent=5, resets_at=1_900_000_000, duration_mins=300),
                secondary=Window(used_percent=1, resets_at=1_900_000_000, duration_mins=10080),
                plan="plus",
            ),
            limits={},
            updated_auth=rotated,
        )

        with mock.patch.object(cli, "query_quota", return_value=snapshot):
            self.assertEqual(cli.cmd_quota("one"), 0)

        self.assertEqual(storage.read_auth(storage.account_path("one"))["tokens"]["refresh_token"], "new")
        self.assertEqual(storage.read_auth(storage.active_auth_path())["tokens"]["refresh_token"], "new")

    def test_login_uses_device_auth_by_default(self) -> None:
        completed = mock.Mock(returncode=0)

        def fake_run(command: list[str], *, check: bool, env: dict[str, str]) -> object:
            self.assertEqual(command, ["codex", "login", "--device-auth"])
            self.assertFalse(storage.active_auth_path().exists())
            storage.write_auth(Path(env["CODEX_HOME"]) / "auth.json", auth_data("fresh", "one@example.com"))
            return completed

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run) as run:
            self.assertEqual(cli.cmd_login("one"), 0)

        self.assertEqual(run.call_count, 1)
        self.assertEqual(storage.read_auth(storage.account_path("one"))["tokens"]["refresh_token"], "fresh")
        self.assertFalse(storage.active_auth_path().exists())

    def test_login_can_use_browser_flow(self) -> None:
        completed = mock.Mock(returncode=0)

        def fake_run(command: list[str], *, check: bool, env: dict[str, str]) -> object:
            self.assertEqual(command, ["codex", "login"])
            storage.write_auth(Path(env["CODEX_HOME"]) / "auth.json", auth_data("fresh", "one@example.com"))
            return completed

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run) as run:
            self.assertEqual(cli.cmd_login("one", device_auth=False), 0)

        self.assertEqual(run.call_count, 1)

    def test_use_best_switches_lowest_usage_account(self) -> None:
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))

        def fake_query(data: dict[str, object]) -> Snapshot:
            tokens = data["tokens"]
            refresh = tokens["refresh_token"]
            used = 9 if refresh == "one" else 1
            return Snapshot(
                email=f"{refresh}@example.com",
                plan="plus",
                auth_method="chatgpt",
                default_limit=RateLimit(
                    limit_id="codex",
                    limit_name=None,
                    primary=Window(used_percent=used, resets_at=1_900_000_000, duration_mins=300),
                    secondary=Window(used_percent=0, resets_at=1_900_000_000, duration_mins=10080),
                    plan="plus",
                ),
                limits={},
                updated_auth=data,
            )

        with mock.patch.object(cli, "query_quota", side_effect=fake_query):
            self.assertEqual(cli.cmd_use_best([]), 0)

        self.assertEqual(storage.identify_active(), "two")

    def test_export_writes_all_accounts(self) -> None:
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))
        output = Path(self.tmp.name) / "export.json"

        self.assertEqual(cli.cmd_export(str(output)), 0)
        exported = json.loads(output.read_text())

        self.assertEqual(sorted(exported), ["one", "two"])
        self.assertEqual(exported["one"]["tokens"]["refresh_token"], "one")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_token_error_detection_reads_codex_stderr(self) -> None:
        self.assertEqual(rpc._token_error("code refresh_token_reused"), "refresh_token_reused")
        self.assertEqual(rpc._clean_rpc_error("body token_invalidated"), "token_invalidated")
        self.assertIsNone(rpc._token_error("ordinary app-server log"))


if __name__ == "__main__":
    unittest.main()
