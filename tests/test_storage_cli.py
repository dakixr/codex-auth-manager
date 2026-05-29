from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest import mock

from codex_auth_manager import cli, engine, storage
from codex_auth_manager import rpc
from codex_auth_manager.display import quota_block
from codex_auth_manager.rpc import RateLimit, Snapshot, Window
from codex_auth_manager.selection import pace_for_window, score_snapshot


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

        with mock.patch.object(engine, "query_quota", return_value=snapshot):
            self.assertEqual(cli.cmd_quota("one"), 0)

        self.assertEqual(storage.read_auth(storage.account_path("one"))["tokens"]["refresh_token"], "new")
        self.assertEqual(storage.read_auth(storage.active_auth_path())["tokens"]["refresh_token"], "new")

    def test_named_quota_marks_active_account(self) -> None:
        storage.write_auth(storage.active_auth_path(), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        snapshot = Snapshot(
            email="one@example.com",
            plan="plus",
            auth_method="chatgpt",
            default_limit=None,
            limits={},
            updated_auth=auth_data("one", "one@example.com"),
        )
        output = StringIO()

        with (
            mock.patch.object(engine, "query_quota", return_value=snapshot),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_quota("one"), 0)

        self.assertTrue(output.getvalue().startswith("\033[1mone *\033[0m\n"))

    def test_quota_display_prints_bars_relative_resets_and_pace_info(self) -> None:
        snapshot = Snapshot(
            email="one@example.com",
            plan="plus",
            auth_method="chatgpt",
            default_limit=RateLimit(
                limit_id="codex",
                limit_name=None,
                primary=Window(used_percent=30, resets_at=1_900_000_000 + 4 * 3600, duration_mins=300),
                secondary=Window(used_percent=10, resets_at=1_900_000_000 + 4 * 24 * 3600, duration_mins=10080),
                plan="plus",
            ),
            limits={},
            updated_auth=auth_data("one", "one@example.com"),
        )

        text = quota_block("one", snapshot, now=1_900_000_000)

        self.assertIn("[######--------------]", text)
        self.assertIn("resets in 4h", text)
        self.assertIn("resets in 4d", text)
        self.assertIn("10% in deficit", text)
        self.assertIn("33% in reserve", text)
        self.assertIn("score 32.1", text)

    def test_quota_without_name_checks_all_accounts(self) -> None:
        storage.write_auth(storage.active_auth_path(), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))

        def fake_query(data: dict[str, object]) -> Snapshot:
            tokens = cast(dict[str, Any], data["tokens"])
            refresh = tokens["refresh_token"]
            updated = auth_data(f"{refresh}-rotated", f"{refresh}@example.com")
            return Snapshot(
                email=f"{refresh}@example.com",
                plan="plus",
                auth_method="chatgpt",
                default_limit=RateLimit(
                    limit_id="codex",
                    limit_name=None,
                    primary=Window(used_percent=1, resets_at=1_900_000_000, duration_mins=300),
                    secondary=Window(used_percent=0, resets_at=1_900_000_000, duration_mins=10080),
                    plan="plus",
                ),
                limits={},
                updated_auth=updated,
            )

        with mock.patch.object(engine, "query_quota", side_effect=fake_query) as query:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.cmd_quota(None), 0)

        self.assertEqual(query.call_count, 2)
        self.assertIn("\033[1mone *\033[0m\n", output.getvalue())
        self.assertIn("\033[1mtwo\033[0m\n", output.getvalue())
        self.assertEqual(storage.read_auth(storage.account_path("one"))["tokens"]["refresh_token"], "one-rotated")
        self.assertEqual(storage.read_auth(storage.active_auth_path())["tokens"]["refresh_token"], "one-rotated")

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

    def test_use_best_switches_best_paced_account(self) -> None:
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))

        def fake_query(data: dict[str, object]) -> Snapshot:
            tokens = cast(dict[str, Any], data["tokens"])
            refresh = tokens["refresh_token"]
            if refresh == "one":
                session_used = 30
                session_reset = 1_900_000_000 + 4 * 3600
                weekly_used = 10
            else:
                session_used = 10
                session_reset = 1_900_000_000 + 4 * 3600
                weekly_used = 80
            return Snapshot(
                email=f"{refresh}@example.com",
                plan="plus",
                auth_method="chatgpt",
                default_limit=RateLimit(
                    limit_id="codex",
                    limit_name=None,
                    primary=Window(used_percent=session_used, resets_at=session_reset, duration_mins=300),
                    secondary=Window(used_percent=weekly_used, resets_at=1_900_000_000 + 4 * 24 * 3600, duration_mins=10080),
                    plan="plus",
                ),
                limits={},
                updated_auth=data,
            )

        with (
            mock.patch.object(engine, "query_quota", side_effect=fake_query),
            mock.patch.object(cli, "score_snapshot", side_effect=lambda snapshot: score_snapshot(snapshot, now=1_900_000_000)),
        ):
            self.assertEqual(cli.cmd_use_best([]), 0)

        self.assertEqual(storage.identify_active(), "one")

    def test_use_best_quota_results_run_in_parallel_and_keep_order(self) -> None:
        started: list[str] = []
        lock = threading.Lock()
        both_started = threading.Event()

        def snapshot_for(name: str) -> Snapshot:
            with lock:
                started.append(name)
                if len(started) == 2:
                    both_started.set()
            if not both_started.wait(timeout=1):
                raise AssertionError("quota probes ran sequentially")
            return Snapshot(
                email=f"{name}@example.com",
                plan="plus",
                auth_method="chatgpt",
                default_limit=RateLimit(
                    limit_id="codex",
                    limit_name=None,
                    primary=Window(used_percent=1, resets_at=1_900_000_000, duration_mins=300),
                    secondary=Window(used_percent=0, resets_at=1_900_000_000, duration_mins=10080),
                    plan="plus",
                ),
                limits={},
                updated_auth=auth_data(name, f"{name}@example.com"),
            )

        with (
            mock.patch.object(engine, "fetch_quota", side_effect=snapshot_for),
        ):
            results = engine.fetch_quotas(["one", "two"], workers=2)

        self.assertEqual([result.name for result in results], ["one", "two"])
        self.assertTrue(all(result.error is None for result in results))

    def test_export_writes_all_accounts(self) -> None:
        storage.write_auth(storage.account_path("one"), auth_data("one", "one@example.com"))
        storage.write_auth(storage.account_path("two"), auth_data("two", "two@example.com"))
        output = Path(self.tmp.name) / "export.json"

        self.assertEqual(cli.cmd_export(str(output)), 0)
        exported = json.loads(output.read_text())

        self.assertEqual(sorted(exported), ["one", "two"])
        self.assertEqual(exported["one"]["tokens"]["refresh_token"], "one")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_import_reads_exported_accounts_without_touching_active_auth(self) -> None:
        storage.write_auth(storage.active_auth_path(), auth_data("active", "active@example.com"))
        source = Path(self.tmp.name) / "import.json"
        source.write_text(json.dumps({
            "one": auth_data("one", "one@example.com"),
            "two": auth_data("two", "two@example.com"),
        }))

        self.assertEqual(cli.cmd_import(source), 0)

        self.assertEqual([account.name for account in storage.list_accounts()], ["one", "two"])
        self.assertEqual(storage.read_auth(storage.account_path("one"))["tokens"]["refresh_token"], "one")
        self.assertEqual(storage.read_auth(storage.account_path("two"))["tokens"]["refresh_token"], "two")
        self.assertEqual(storage.read_auth(storage.active_auth_path())["tokens"]["refresh_token"], "active")

    def test_import_rejects_non_object_export(self) -> None:
        source = Path(self.tmp.name) / "bad.json"
        source.write_text("[]")

        with self.assertRaises(storage.StorageError):
            cli.cmd_import(source)

    def test_token_error_detection_reads_codex_stderr(self) -> None:
        self.assertEqual(rpc._token_error("code refresh_token_reused"), "refresh_token_reused")
        self.assertEqual(rpc._clean_rpc_error("body token_invalidated"), "token_invalidated")
        self.assertIsNone(rpc._token_error("ordinary app-server log"))

    def test_query_quota_uses_access_token_without_refresh(self) -> None:
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: int) -> object:
            requests.append(request)
            self.assertEqual(timeout, 30)
            return _Response({
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 22,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 43,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 604_800,
                    },
                },
            })

        with mock.patch.object(rpc.urllib.request, "urlopen", side_effect=fake_urlopen):
            snapshot = rpc.query_quota(auth_data("refresh", "one@example.com"))

        self.assertEqual(len(requests), 1)
        request = cast(Any, requests[0])
        self.assertEqual(request.full_url, rpc.USAGE_URL)
        self.assertTrue(request.get_header("Authorization").startswith("Bearer header."))
        self.assertEqual(request.get_header("Chatgpt-account-id"), "one@example.com")
        self.assertEqual(snapshot.email, "one@example.com")
        self.assertEqual(
            snapshot.default_limit.primary.used_percent if snapshot.default_limit and snapshot.default_limit.primary else None,
            22,
        )
        self.assertEqual(
            snapshot.default_limit.secondary.used_percent if snapshot.default_limit and snapshot.default_limit.secondary else None,
            43,
        )

    def test_query_quota_refreshes_after_unauthorized_usage_response(self) -> None:
        requests: list[object] = []

        def fake_urlopen(request: object, timeout: int) -> object:
            requests.append(request)
            url = cast(Any, request).full_url
            if url == rpc.USAGE_URL and len(requests) == 1:
                raise urllib.error.HTTPError(
                    rpc.USAGE_URL,
                    401,
                    "Unauthorized",
                    {},
                    _BytesResponse({"error": {"code": "token_expired"}}),
                )
            if url == rpc.TOKEN_URL:
                tokens = cast(dict[str, str], auth_data("fresh", "one@example.com")["tokens"])
                return _Response({
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "id_token": tokens["id_token"],
                })
            return _Response({
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 5,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 18_000,
                    },
                },
            })

        with mock.patch.object(rpc.urllib.request, "urlopen", side_effect=fake_urlopen):
            snapshot = rpc.query_quota(auth_data("refresh", "one@example.com"))

        self.assertEqual([cast(Any, request).full_url for request in requests], [rpc.USAGE_URL, rpc.TOKEN_URL, rpc.USAGE_URL])
        self.assertEqual(cast(dict[str, Any], snapshot.updated_auth["tokens"])["access_token"], "fresh-access")
        self.assertEqual(cast(dict[str, Any], snapshot.updated_auth["tokens"])["refresh_token"], "fresh-refresh")

    def test_query_quota_maps_free_weekly_only_window_to_secondary(self) -> None:
        def fake_urlopen(request: object, timeout: int) -> object:
            return _Response({
                "plan_type": "free",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 0,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 604_800,
                    },
                    "secondary_window": None,
                },
            })

        with mock.patch.object(rpc.urllib.request, "urlopen", side_effect=fake_urlopen):
            snapshot = rpc.query_quota(auth_data("refresh", "one@example.com"))

        self.assertIsNone(snapshot.default_limit.primary if snapshot.default_limit else None)
        self.assertEqual(
            snapshot.default_limit.secondary.used_percent if snapshot.default_limit and snapshot.default_limit.secondary else None,
            0,
        )
        self.assertEqual(
            snapshot.default_limit.secondary.duration_mins if snapshot.default_limit and snapshot.default_limit.secondary else None,
            10080,
        )

    def test_pace_reports_reserve_and_deficit(self) -> None:
        now = 1_900_000_000
        reserve = pace_for_window(
            Window(used_percent=10, resets_at=now + 4 * 24 * 3600, duration_mins=10080),
            now=now,
            default_window_minutes=10080,
        )
        deficit = pace_for_window(
            Window(used_percent=50, resets_at=now + 4 * 24 * 3600, duration_mins=10080),
            now=now,
            default_window_minutes=10080,
        )

        self.assertIsNotNone(reserve)
        self.assertIsNotNone(deficit)
        assert reserve is not None
        assert deficit is not None
        self.assertAlmostEqual(reserve.expected_used_percent, 42.857, places=2)
        self.assertAlmostEqual(reserve.delta_percent, -32.857, places=2)
        self.assertIn("in reserve", reserve.label)
        self.assertIn("lasts until reset", reserve.label)
        self.assertAlmostEqual(deficit.delta_percent, 7.143, places=2)
        self.assertIn("in deficit", deficit.label)
        self.assertIn("projected empty", deficit.label)

    def test_pace_suppresses_eta_too_early_in_window(self) -> None:
        now = 1_900_000_000
        pace = pace_for_window(
            Window(used_percent=1, resets_at=now + 5 * 3600 - 60, duration_mins=300),
            now=now,
            default_window_minutes=300,
        )

        self.assertIsNotNone(pace)
        assert pace is not None
        self.assertLess(pace.expected_used_percent, 3)
        self.assertIsNone(pace.eta_seconds)
        self.assertFalse(pace.will_last_to_reset)
        self.assertEqual(pace.label, "on pace")

    def test_score_rejects_exhausted_weekly_window(self) -> None:
        now = 1_900_000_000
        snapshot = Snapshot(
            email="one@example.com",
            plan="plus",
            auth_method="chatgpt",
            default_limit=RateLimit(
                limit_id="codex",
                limit_name=None,
                primary=Window(used_percent=10, resets_at=now + 4 * 3600, duration_mins=300),
                secondary=Window(used_percent=98, resets_at=now + 4 * 24 * 3600, duration_mins=10080),
                plan="plus",
            ),
            limits={},
            updated_auth={},
        )

        score = score_snapshot(snapshot, now=now)

        self.assertTrue(score.rejected)
        self.assertEqual(score.reject_reason, "weekly >= 98%")


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class _BytesResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
