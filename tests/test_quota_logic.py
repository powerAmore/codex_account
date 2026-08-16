import datetime
import json
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
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
            {
                "kind": "http",
                "status": 200,
                "body": {
                    "rate_limit": rate_limit,
                    "plan_type": "plus",
                    "email": "a@example.com",
                },
            }
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["rate_limit"], rate_limit)
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(result["email"], "a@example.com")

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
        self.assertEqual(unauthorized["status"], "Token Rejected")
        self.assertTrue(unauthorized["auth_failure_confirmed"])
        self.assertEqual(forbidden["status"], "Blocked by Cloudflare")

    def test_html_401_is_not_misreported_as_expired_token(self):
        result = codex_mgr._parse_quota_fetch_result(
            {
                "kind": "http",
                "status": 401,
                "content_type": "text/html; charset=UTF-8",
                "body": "<!doctype html><title>Just a moment...</title>",
            }
        )
        self.assertEqual(result["status"], "Blocked by Cloudflare")
        self.assertNotIn("auth_failure_confirmed", result)

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

    def test_bearer_query_falls_back_from_cloudflare_path(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"rate_limit": {"primary_window": {}}}).encode()

        blocked = urllib.error.HTTPError(
            "https://chatgpt.com/backend-api/codex/usage",
            403,
            "Forbidden",
            {"cf-ray": "test-ray"},
            io.BytesIO(b"challenge"),
        )
        opener = mock.Mock()
        opener.open.side_effect = [blocked, FakeResponse()]
        with mock.patch("codex_mgr._url_opener_for_network_mode", return_value=opener):
            result = codex_mgr._query_quota_with_token("test-token")

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["quota_endpoint"], "wham/usage")
        self.assertTrue(result["quota_fallback_used"])
        self.assertEqual(result["quota_endpoints_tried"], ["codex/usage", "wham/usage"])

    def test_bearer_query_falls_back_from_single_endpoint_401(self):
        class FakeResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"rate_limit": {"primary_window": {}}}).encode()

        rejected = urllib.error.HTTPError(
            "https://chatgpt.com/backend-api/codex/usage",
            401,
            "Unauthorized",
            {"content-type": "application/json"},
            io.BytesIO(b'{"detail":"Unauthorized"}'),
        )
        opener = mock.Mock()
        opener.open.side_effect = [rejected, FakeResponse()]
        with mock.patch("codex_mgr._url_opener_for_network_mode", return_value=opener):
            result = codex_mgr._query_quota_with_token("test-token")

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["quota_endpoint"], "wham/usage")

    def test_mixed_401_and_cloudflare_is_inconclusive(self):
        rejected = urllib.error.HTTPError(
            "https://chatgpt.com/backend-api/codex/usage",
            401,
            "Unauthorized",
            {"content-type": "application/json"},
            io.BytesIO(b'{"detail":"Unauthorized"}'),
        )
        blocked = urllib.error.HTTPError(
            "https://chatgpt.com/backend-api/wham/usage",
            403,
            "Forbidden",
            {"content-type": "text/html", "cf-ray": "test-ray"},
            io.BytesIO(b"<!doctype html><title>challenge</title>"),
        )
        opener = mock.Mock()
        opener.open.side_effect = [rejected, blocked]
        with mock.patch("codex_mgr._url_opener_for_network_mode", return_value=opener):
            result = codex_mgr._query_quota_with_token("test-token")

        self.assertTrue(result["status"].startswith("Auth Check Inconclusive"))
        self.assertFalse(result["auth_failure_confirmed"])

    def test_two_structured_401_responses_confirm_token_rejection(self):
        def rejected(url):
            return urllib.error.HTTPError(
                url,
                401,
                "Unauthorized",
                {"content-type": "application/json"},
                io.BytesIO(b'{"detail":"Unauthorized"}'),
            )

        opener = mock.Mock()
        opener.open.side_effect = [
            rejected("https://chatgpt.com/backend-api/codex/usage"),
            rejected("https://chatgpt.com/backend-api/wham/usage"),
        ]
        with mock.patch("codex_mgr._url_opener_for_network_mode", return_value=opener):
            result = codex_mgr._query_quota_with_token("test-token")

        self.assertEqual(result["status"], "Token Rejected")
        self.assertTrue(result["auth_failure_confirmed"])
        self.assertEqual(
            result["quota_endpoint_statuses"], ["Token Rejected", "Token Rejected"]
        )

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

    def test_quota_transient_failure_is_retried(self):
        responses = [
            {"status": "Blocked by Cloudflare", "auth_status": "OAuth Probe Timeout"},
            {"status": "OK", "auth_status": "OK"},
        ]
        with mock.patch("codex_mgr._query_quota_once", side_effect=responses):
            result = codex_mgr.silent_query_quota("profile", max_attempts=2)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["auth_status"], "OK")
        self.assertEqual(result["attempts"], 2)

    def test_token_rejection_requires_two_confirmations(self):
        rejected = {
            "status": "Token Rejected",
            "auth_status": "OAuth Network Error: websocket failed",
            "auth_failure_confirmed": True,
        }
        with mock.patch("codex_mgr._query_quota_once", side_effect=lambda *_args, **_kwargs: dict(rejected)):
            once = codex_mgr.silent_query_quota("profile", max_attempts=1)
            twice = codex_mgr.silent_query_quota("profile", max_attempts=2)
        self.assertFalse(once["auth_failure_confirmed"])
        self.assertTrue(twice["auth_failure_confirmed"])

    def test_quota_success_with_oauth_network_warning_is_usable(self):
        info = {
            "status": "OK",
            "auth_status": "OAuth Network Error: websocket failed",
        }
        self.assertTrue(codex_mgr._is_query_ok(info))

    def test_https_quota_success_separates_websocket_warning(self):
        result = codex_mgr._combine_quota_and_oauth_result(
            {"status": "OK", "rate_limit": {}},
            {"status": "OAuth Network Error: websocket timed out"},
        )
        self.assertEqual(result["auth_status"], "OK")
        self.assertEqual(result["auth_transport"], "https")
        self.assertIn("websocket", result["websocket_warning"])

    def test_subscription_response_extracts_plan_and_until(self):
        result = codex_mgr._parse_subscription_fetch_result(
            {
                "kind": "http",
                "status": 200,
                "body": {
                    "plan_type": "plus",
                    "active_until": "2026-08-01T19:38:18Z",
                    "active_start": "2026-07-01T19:38:08Z",
                    "will_renew": False,
                    "is_delinquent": True,
                    "grace_period_end_timestamp": "2026-08-04T19:38:08Z",
                    "billing_period": "monthly",
                },
            }
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(result["subscription_until"], "2026-08-01T19:38:18Z")
        self.assertFalse(result["will_renew"])
        self.assertTrue(result["is_delinquent"])
        self.assertEqual(result["grace_period_end"], "2026-08-04T19:38:08Z")

    def test_subscription_merge_failure_does_not_break_quota(self):
        merged = codex_mgr._merge_subscription_into_quota_result(
            {"status": "OK", "rate_limit": {}, "plan_type": "plus"},
            {"status": "Fetch Timeout"},
        )
        self.assertEqual(merged["status"], "OK")
        self.assertEqual(merged["plan_type"], "plus")
        self.assertEqual(merged["subscription_status"], "Fetch Timeout")
        self.assertNotIn("subscription_until", merged)

    def test_subscription_merge_success_overrides_plan_and_until(self):
        merged = codex_mgr._merge_subscription_into_quota_result(
            {"status": "OK", "rate_limit": {}, "plan_type": "plus"},
            {
                "status": "OK",
                "plan_type": "plus",
                "subscription_until": "2026-09-01T00:00:00Z",
                "will_renew": True,
            },
        )
        self.assertEqual(merged["subscription_until"], "2026-09-01T00:00:00Z")
        self.assertTrue(merged["will_renew"])
        self.assertEqual(merged["subscription_status"], "OK")

    def test_format_subscription_until_marks_grace_and_nonrenew(self):
        now = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        self.assertEqual(
            codex_mgr._format_subscription_until(
                {
                    "subscription_until": "2026-08-01T19:38:18Z",
                    "is_delinquent": True,
                    "grace_period_end": "2026-08-04T19:38:08Z",
                },
                now=now,
            ),
            "2026-08-01 宽限08-04",
        )
        self.assertEqual(
            codex_mgr._format_subscription_until(
                {
                    "subscription_until": "2026-08-21T10:12:37Z",
                    "will_renew": False,
                },
                now=now,
            ),
            "2026-08-21 不续费",
        )
        self.assertEqual(
            codex_mgr._format_subscription_until(
                {"subscription_until": "2026-08-14T15:42:00Z", "will_renew": False},
                now=datetime.datetime(2026, 8, 16, tzinfo=datetime.timezone.utc),
            ),
            "2026-08-14 已到期",
        )

    def test_expired_plus_reconciles_to_free(self):
        now = datetime.datetime(2026, 8, 16, 8, 0, tzinfo=datetime.timezone.utc)
        plan, until = codex_mgr._reconcile_plan_and_until(
            "FREE",
            "2026-08-14T15:42:00+00:00",
            {
                "plan_type": "plus",
                "subscription_status": "OK",
                "will_renew": False,
                "is_delinquent": False,
            },
            now=now,
        )
        self.assertEqual(plan, "FREE")
        self.assertEqual(until, "2026-08-14 已到期")

    def test_stale_plus_without_until_follows_jwt_free(self):
        now = datetime.datetime(2026, 8, 16, 8, 0, tzinfo=datetime.timezone.utc)
        plan, until = codex_mgr._reconcile_plan_and_until(
            "FREE",
            "Unknown",
            {
                "plan_type": "plus",
                "subscription_status": "OK",
                "subscription_until": None,
                "will_renew": False,
            },
            now=now,
        )
        self.assertEqual(plan, "FREE")
        self.assertEqual(until, "—")

    def test_active_plus_is_not_downgraded_by_old_jwt(self):
        now = datetime.datetime(2026, 8, 16, 8, 0, tzinfo=datetime.timezone.utc)
        plan, until = codex_mgr._reconcile_plan_and_until(
            "FREE",
            "2026-08-14T15:42:00+00:00",
            {
                "plan_type": "plus",
                "subscription_until": "2026-09-04T00:00:00Z",
                "will_renew": False,
            },
            now=now,
        )
        self.assertEqual(plan, "PLUS")
        self.assertEqual(until, "2026-09-04 不续费")

    def test_subscription_null_until_is_kept(self):
        result = codex_mgr._parse_subscription_fetch_result(
            {
                "kind": "http",
                "status": 200,
                "body": {
                    "plan_type": "plus",
                    "active_until": None,
                    "will_renew": False,
                },
            }
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["plan_type"], "plus")
        self.assertIsNone(result["subscription_until"])
        self.assertFalse(result["will_renew"])

    def test_ssl_handshake_timeout_is_classified(self):
        self.assertEqual(
            codex_mgr._network_failure_status(
                TimeoutError("_ssl.c:1003: The handshake operation timed out")
            ),
            "Fetch Timeout: SSL handshake",
        )
        self.assertEqual(
            codex_mgr._network_failure_status(TimeoutError("The read operation timed out")),
            "Fetch Timeout",
        )
        self.assertTrue(
            codex_mgr._is_transient_query_status("Fetch Timeout: SSL handshake")
        )

    def test_display_pad_aligns_cjk_subscription_until(self):
        width = 22
        samples = [
            "2026-08-01 宽限08-04",
            "2026-08-21",
            "2026-08-14 不续费",
            "2026-08-17 不续费",
        ]
        padded = [codex_mgr._pad_display(s, width) for s in samples]
        self.assertTrue(all(codex_mgr._display_width(p) == width for p in padded))
        # Quota 起点应一致：前缀显示宽度相同
        self.assertEqual(len({codex_mgr._display_width(p) for p in padded}), 1)


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

    def test_quota_success_preserves_previous_subscription_on_sub_failure(self):
        cache = {
            "profile": {
                "limits": {
                    "status": "OK",
                    "rate_limit": {},
                    "plan_type": "plus",
                    "subscription_until": "2026-08-21T10:12:37Z",
                    "subscription_status": "OK",
                    "will_renew": True,
                },
                "timestamp": 100,
            }
        }
        codex_mgr._record_usage_result(
            cache,
            "profile",
            {
                "status": "OK",
                "rate_limit": {"primary_window": {}},
                "plan_type": "plus",
                "subscription_status": "Fetch Timeout",
            },
            timestamp=200,
        )
        limits = cache["profile"]["limits"]
        self.assertEqual(limits["subscription_until"], "2026-08-21T10:12:37Z")
        self.assertTrue(limits["will_renew"])
        self.assertEqual(limits["subscription_status"], "Fetch Timeout")

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

    def test_websocket_warning_still_promotes_rotated_credentials(self):
        report = {
            "checks": {
                "auth.credentials": {"status": "ok", "summary": "auth configured"},
                "network.websocket_reachability": {
                    "status": "warning",
                    "summary": "Responses WebSocket failed",
                },
            },
        }

        def run_doctor(*_args, **kwargs):
            temp_auth = pathlib.Path(kwargs["env"]["CODEX_HOME"], "auth.json")
            auth = json.loads(temp_auth.read_text())
            auth["last_refresh"] = "2026-08-08T10:00:00Z"
            auth["tokens"]["refresh_token"] = "rotated"
            temp_auth.write_text(json.dumps(auth))
            return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            profile_dir = prefix + "_test"
            os.makedirs(profile_dir)
            auth_path = pathlib.Path(profile_dir, "auth.json")
            auth_path.write_text(json.dumps({
                "last_refresh": "2026-08-07T10:00:00Z",
                "tokens": {
                    "account_id": "acct-1",
                    "access_token": "token",
                    "refresh_token": "old",
                },
            }))
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr.CODEX_CLI_PATH", "/usr/bin/true"),
                mock.patch("codex_mgr.subprocess.run", side_effect=run_doctor),
            ):
                result = codex_mgr._probe_codex_oauth("test")

            saved = json.loads(auth_path.read_text())
        self.assertTrue(result["credentials_refreshed"])
        self.assertTrue(result["status"].startswith("OAuth Network Error"))
        self.assertEqual(saved["tokens"]["refresh_token"], "rotated")


class CredentialSyncTests(unittest.TestCase):
    def test_older_snapshot_cannot_overwrite_newer_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = pathlib.Path(temp_dir, "source.json")
            target = pathlib.Path(temp_dir, "target.json")
            source.write_text(json.dumps({
                "last_refresh": "2026-08-01T00:00:00Z",
                "tokens": {"account_id": "acct-1", "refresh_token": "old"},
            }))
            target.write_text(json.dumps({
                "last_refresh": "2026-08-02T00:00:00Z",
                "tokens": {"account_id": "acct-1", "refresh_token": "new"},
            }))
            ok, action = codex_mgr._sync_auth_snapshot(str(source), str(target))
            saved = json.loads(target.read_text())
        self.assertTrue(ok)
        self.assertEqual(action, "destination-newer")
        self.assertEqual(saved["tokens"]["refresh_token"], "new")

    def test_different_account_cannot_overwrite_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = pathlib.Path(temp_dir, "source.json")
            target = pathlib.Path(temp_dir, "target.json")
            source.write_text(json.dumps({"tokens": {"account_id": "acct-a"}}))
            target.write_text(json.dumps({"tokens": {"account_id": "acct-b"}}))
            ok, action = codex_mgr._sync_auth_snapshot(str(source), str(target))
        self.assertFalse(ok)
        self.assertEqual(action, "identity-mismatch")


class ProfileNamingTests(unittest.TestCase):
    def test_add_new_uses_detected_name_instead_of_literal_new(self):
        def jwt(payload):
            import base64
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return f"header.{encoded}.signature"

        with tempfile.TemporaryDirectory() as temp_dir:
            auth_file = os.path.join(temp_dir, "auth.json")
            pathlib.Path(auth_file).write_text(json.dumps({
                "tokens": {"id_token": jwt({"name": "Jane Doe", "email": "new@example.com"})},
            }))
            prefix = os.path.join(temp_dir, "Profile")
            with (
                mock.patch("codex_mgr.AUTH_FILE", auth_file),
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr.get_profiles", return_value=[]),
                mock.patch("codex_mgr.get_current_active", return_value=None),
                mock.patch("codex_mgr.kill_chatgpt_processes"),
                mock.patch("codex_mgr.backup_profile", return_value=True) as backup,
                mock.patch("codex_mgr.set_current_active") as set_active,
                mock.patch("codex_mgr.subprocess.run"),
            ):
                codex_mgr.cmd_add("new")

        self.assertEqual(backup.call_args_list[-1].args[0], "Jane_Doe")
        set_active.assert_called_once_with("Jane_Doe")

    def test_rename_moves_profile_cache_and_active_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            source = prefix + "_new"
            target = prefix + "_Jane_Doe"
            os.makedirs(source)
            cache_file = os.path.join(temp_dir, "usage.json")
            active_file = os.path.join(temp_dir, "active")
            pathlib.Path(cache_file).write_text(json.dumps({"new": {"limits": {"status": "OK"}}}))
            pathlib.Path(active_file).write_text("new")
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr.USAGE_CACHE_FILE", cache_file),
                mock.patch("codex_mgr.ACTIVE_FILE", active_file),
            ):
                renamed = codex_mgr.cmd_rename("new", "Jane_Doe")
            cache = json.loads(pathlib.Path(cache_file).read_text())
            target_exists = os.path.isdir(target)
            active_value = pathlib.Path(active_file).read_text()

        self.assertTrue(renamed)
        self.assertTrue(target_exists)
        self.assertIn("Jane_Doe", cache)
        self.assertNotIn("new", cache)
        self.assertEqual(active_value, "Jane_Doe")


class ProbeSchedulingTests(unittest.TestCase):
    def test_healthy_far_from_expiry_skips_oauth_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            profile_dir = prefix + "_test"
            os.makedirs(profile_dir)
            pathlib.Path(profile_dir, "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct-1", "access_token": "token"},
            }))
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr._query_quota_with_token", return_value={"status": "OK", "rate_limit": {}}),
                mock.patch("codex_mgr._access_token_needs_refresh", return_value=False),
                mock.patch("codex_mgr._probe_codex_oauth") as oauth_probe,
            ):
                result = codex_mgr._query_quota_once("test", include_subscription=False)
        self.assertEqual(result["status"], "OK")
        oauth_probe.assert_not_called()

    def test_subscription_is_queried_when_quota_endpoint_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            profile_dir = prefix + "_test"
            os.makedirs(profile_dir)
            pathlib.Path(profile_dir, "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct-1", "access_token": "token"},
            }))
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch(
                    "codex_mgr._query_quota_with_token",
                    return_value={"status": "Blocked by Cloudflare"},
                ),
                mock.patch(
                    "codex_mgr._query_subscription_with_token",
                    return_value={"status": "OK", "plan_type": "plus"},
                ) as subscription_query,
            ):
                result = codex_mgr._query_quota_once(
                    "test", probe_oauth=False, include_subscription=True
                )

        self.assertEqual(result["status"], "Blocked by Cloudflare")
        self.assertEqual(result["subscription_status"], "OK")
        subscription_query.assert_called_once_with("token", "acct-1")

    def test_active_profile_never_refreshes_copied_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "Profile")
            profile_dir = prefix + "_test"
            os.makedirs(profile_dir)
            pathlib.Path(profile_dir, "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct-1", "access_token": "token"},
            }))
            with (
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr._query_quota_with_token", return_value={
                    "status": "Token Rejected",
                    "auth_failure_confirmed": True,
                }),
                mock.patch("codex_mgr._probe_codex_oauth") as oauth_probe,
            ):
                result = codex_mgr._query_quota_once(
                    "test",
                    probe_oauth=False,
                    include_subscription=False,
                )
        self.assertEqual(result["status"], "Token Rejected")
        oauth_probe.assert_not_called()


class StatePromotionTests(unittest.TestCase):
    def test_restore_removes_previous_accounts_storage_and_cookie_sidecars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = os.path.join(temp_dir, "Codex")
            default_dir = os.path.join(app_dir, "Default")
            prefix = os.path.join(temp_dir, "Profile")
            backup_dir = prefix + "_target"
            backup_default = os.path.join(backup_dir, "app_support", "Default")
            auth_file = os.path.join(temp_dir, ".codex", "auth.json")
            active_file = os.path.join(temp_dir, "active")
            os.makedirs(os.path.join(default_dir, "Local Storage"))
            os.makedirs(os.path.join(default_dir, "Session Storage"))
            pathlib.Path(default_dir, "Cookies-wal").write_text("stale")
            pathlib.Path(default_dir, "Cookies-shm").write_text("stale")
            os.makedirs(backup_default)
            pathlib.Path(backup_dir, "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct-target"},
            }))
            with (
                mock.patch("codex_mgr.APP_SUPPORT_DIR", app_dir),
                mock.patch("codex_mgr.BACKUP_PREFIX", prefix),
                mock.patch("codex_mgr.AUTH_FILE", auth_file),
                mock.patch("codex_mgr.ACTIVE_FILE", active_file),
            ):
                restored = codex_mgr.restore_profile("target")

            self.assertTrue(restored)
            self.assertFalse(os.path.exists(os.path.join(default_dir, "Local Storage")))
            self.assertFalse(os.path.exists(os.path.join(default_dir, "Session Storage")))
            self.assertFalse(os.path.exists(os.path.join(default_dir, "Cookies-wal")))
            self.assertFalse(os.path.exists(os.path.join(default_dir, "Cookies-shm")))

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
