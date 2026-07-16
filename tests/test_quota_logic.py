import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import codex_mgr


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.timeout = None

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self):
        return json.dumps(self.frames.pop(0))


class QuotaParsingTests(unittest.TestCase):
    def test_success_response(self):
        rate_limit = {"primary_window": {"used_percent": 12}}
        result = codex_mgr._parse_quota_fetch_result(
            {"kind": "http", "status": 200, "body": {"rate_limit": rate_limit}}
        )
        self.assertEqual(result, {"status": "OK", "rate_limit": rate_limit})

    def test_abort_is_fetch_timeout(self):
        result = codex_mgr._parse_quota_fetch_result(
            {"kind": "fetch_error", "name": "AbortError", "message": "signal aborted"}
        )
        self.assertEqual(result["status"], "Fetch Timeout")

    def test_http_statuses_are_preserved(self):
        unauthorized = codex_mgr._parse_quota_fetch_result(
            {"kind": "http", "status": 401, "body": {"detail": "Unauthorized"}}
        )
        forbidden = codex_mgr._parse_quota_fetch_result(
            {"kind": "http", "status": 403, "body": "challenge"}
        )
        self.assertEqual(unauthorized["status"], "Token Expired")
        self.assertEqual(forbidden["status"], "Blocked by Cloudflare")

    def test_cdp_request_ignores_events(self):
        ws = FakeWebSocket(
            [
                {"method": "Page.loadEventFired", "params": {}},
                {"id": 1, "result": {"value": 42}},
            ]
        )
        response = codex_mgr._cdp_request(ws, [0], "Runtime.evaluate")
        self.assertEqual(response["result"]["value"], 42)
        self.assertEqual(ws.sent[0]["id"], 1)

    def test_direct_bearer_query_uses_common_parser(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"rate_limit": {"primary_window": {}}}).encode()

        opener = mock.Mock()
        opener.open.return_value = FakeResponse()
        with mock.patch("codex_mgr._url_opener_for_network_mode", return_value=opener):
            result = codex_mgr._query_quota_with_token("test-token")

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source"], "bearer")

    def test_tun_mode_disables_application_proxy(self):
        with mock.patch.dict(os.environ, {"CODEX_MGR_NETWORK_MODE": "tun"}):
            self.assertEqual(codex_mgr.get_network_mode(), "tun")
            self.assertEqual(codex_mgr._browser_proxy_args(), ["--no-proxy-server"])

    def test_env_mode_leaves_proxy_resolution_enabled(self):
        with mock.patch.dict(os.environ, {"CODEX_MGR_NETWORK_MODE": "env"}):
            self.assertEqual(codex_mgr.get_network_mode(), "env")
            self.assertEqual(codex_mgr._browser_proxy_args(), [])

    def test_heartbeat_identity_must_match(self):
        heartbeat = {"account_id": "acct-1", "email": "User@Example.com"}
        self.assertEqual(
            codex_mgr._validate_heartbeat_identity(
                heartbeat, "acct-1", "user@example.com"
            ),
            (True, "OK"),
        )
        ok, status = codex_mgr._validate_heartbeat_identity(
            heartbeat, "acct-2", "user@example.com"
        )
        self.assertFalse(ok)
        self.assertIn("Mismatch", status)

    def test_oauth_probe_failure_is_retried(self):
        responses = [
            {"status": "OK", "auth_status": "OAuth Probe Timeout"},
            {"status": "OK", "auth_status": "OK"},
        ]
        with mock.patch("codex_mgr._query_quota_once", side_effect=responses):
            result = codex_mgr.silent_query_quota("profile", max_attempts=2)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["auth_status"], "OK")
        self.assertEqual(result["attempts"], 2)


class UsageCacheTests(unittest.TestCase):
    def test_failure_preserves_last_success(self):
        cache = {
            "profile": {
                "limits": {"status": "OK", "rate_limit": {"primary_window": {}}},
                "timestamp": 100,
            }
        }
        codex_mgr._record_usage_result(
            cache,
            "profile",
            {"status": "Fetch Timeout", "attempts": 2},
            timestamp=200,
        )
        self.assertEqual(cache["profile"]["limits"]["status"], "OK")
        self.assertEqual(cache["profile"]["timestamp"], 100)
        self.assertEqual(
            cache["profile"]["last_error"]["limits"]["status"],
            "Fetch Timeout",
        )

    def test_failure_is_stored_without_previous_success(self):
        cache = {}
        codex_mgr._record_usage_result(
            cache,
            "profile",
            {"status": "Fetch Timeout"},
            timestamp=200,
        )
        self.assertEqual(cache["profile"]["limits"]["status"], "Fetch Timeout")
        self.assertNotIn("last_error", cache["profile"])

    def test_success_clears_previous_error(self):
        cache = {
            "profile": {
                "limits": {"status": "OK"},
                "timestamp": 100,
                "last_error": {"limits": {"status": "Fetch Timeout"}, "timestamp": 150},
            }
        }
        codex_mgr._record_usage_result(
            cache,
            "profile",
            {"status": "OK", "rate_limit": {}},
            timestamp=200,
        )
        self.assertEqual(cache["profile"]["timestamp"], 200)
        self.assertNotIn("last_error", cache["profile"])

    def test_cache_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "usage.json")
            with mock.patch("codex_mgr.USAGE_CACHE_FILE", cache_path):
                codex_mgr.save_usage_cache({"profile": {"limits": {"status": "OK"}}})
            with open(cache_path) as cache_file:
                saved = json.load(cache_file)
            self.assertEqual(saved["profile"]["limits"]["status"], "OK")
            self.assertEqual(os.stat(cache_path).st_mode & 0o777, 0o600)


class OAuthProbeTests(unittest.TestCase):
    def test_unrelated_doctor_failure_does_not_fail_auth_probe(self):
        report = {
            "overallStatus": "fail",
            "checks": {
                "auth.credentials": {"status": "ok", "summary": "auth configured"},
                "network.websocket_reachability": {
                    "status": "ok",
                    "summary": "authenticated websocket succeeded",
                },
                "updates.status": {"status": "fail", "summary": "update check failed"},
            },
        }
        completed = mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            profile_dir = prefix + "_test"
            os.makedirs(profile_dir)
            auth = {
                "tokens": {
                    "account_id": "acct-1",
                    "access_token": "token",
                    "refresh_token": "refresh",
                }
            }
            pathlib.Path(profile_dir, "auth.json").write_text(json.dumps(auth))
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr.CODEX_CLI_PATH", "/usr/bin/true"),
                mock.patch("codex_mgr.subprocess.run", return_value=completed),
            ):
                result = codex_mgr._probe_codex_oauth("test")

        self.assertEqual(result["status"], "OK")


class StatePromotionTests(unittest.TestCase):
    def test_valid_cookie_snapshot_is_promoted_with_rollback_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source")
            backup = os.path.join(temp_dir, "profile", "Default")
            os.makedirs(source)
            os.makedirs(backup)
            pathlib.Path(backup, "sentinel").write_text("previous")
            connection = sqlite3.connect(os.path.join(source, "Cookies"))
            connection.execute("CREATE TABLE cookies (host_key TEXT)")
            connection.execute("INSERT INTO cookies VALUES ('.chatgpt.com')")
            connection.commit()
            connection.close()

            promoted = codex_mgr._promote_browser_state(source, backup)

            self.assertTrue(promoted)
            self.assertTrue(os.path.exists(os.path.join(backup, "Cookies")))
            self.assertEqual(
                pathlib.Path(backup + ".previous", "sentinel").read_text(),
                "previous",
            )

    def test_invalid_cookie_snapshot_never_replaces_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source")
            backup = os.path.join(temp_dir, "profile", "Default")
            os.makedirs(source)
            os.makedirs(backup)
            pathlib.Path(source, "Cookies").write_bytes(b"not sqlite")
            pathlib.Path(backup, "sentinel").write_text("keep")

            promoted = codex_mgr._promote_browser_state(source, backup)

            self.assertFalse(promoted)
            self.assertEqual(pathlib.Path(backup, "sentinel").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
