import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.keychain import KeychainError
from openusage_bar.network import AuthenticationRequired, NetworkError, RateLimited
from openusage_bar.kiro import (
    KIRO_SOCIAL_SERVICE,
    KiroCredentialError,
    KiroParseError,
    KiroQuotaAdapter,
    SecurityKiroTokenReader,
    parse_kiro_credentials,
    parse_kiro_quota,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
RESET = 1784073600
SECRET = "secret-token-must-never-be-logged"
PROFILE = "arn:aws:codewhisperer:us-east-1:123:profile/demo"


def credential_json(**overrides):
    payload = {
        "access_token": SECRET,
        "profile_arn": PROFILE,
        "refresh_token": "refresh-token-must-never-be-returned",
    }
    payload.update(overrides)
    return json.dumps(payload)


def quota_payload(**row_overrides):
    row = {
        "displayName": "Credit",
        "resourceType": "CREDIT",
        "currentUsageWithPrecision": 12.5,
        "usageLimitWithPrecision": 100.0,
        "nextDateReset": RESET,
    }
    row.update(row_overrides)
    return {
        "subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
        "nextDateReset": RESET + 10,
        "usageBreakdownList": [row],
    }


class KiroCredentialTests(unittest.TestCase):
    def test_parse_social_credentials_extracts_only_required_fields(self):
        credentials = parse_kiro_credentials(credential_json())

        self.assertEqual(credentials.access_token, SECRET)
        self.assertEqual(credentials.region, "us-east-1")
        self.assertEqual(credentials.profile_arn, PROFILE)
        self.assertFalse(hasattr(credentials, "refresh_token"))

    def test_accepts_camel_case_social_schema(self):
        credentials = parse_kiro_credentials(
            json.dumps({"accessToken": SECRET, "profileArn": PROFILE})
        )

        self.assertEqual(credentials.region, "us-east-1")

    def test_region_parser_rejects_hostname_injection(self):
        malicious = "arn:aws:codewhisperer:us-east-1.evil.example:123:profile/demo"
        with self.assertRaises(KiroCredentialError) as raised:
            parse_kiro_credentials(credential_json(profile_arn=malicious))

        self.assertNotIn("evil", str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))

    def test_rejects_invalid_json_and_missing_required_fields_without_secrets(self):
        cases = (
            "not-json-" + SECRET,
            json.dumps({"access_token": SECRET}),
            json.dumps({"profile_arn": PROFILE, "access_token": ""}),
            json.dumps([]),
        )
        for raw in cases:
            with self.subTest(raw_type=type(raw).__name__):
                with self.assertRaises(KiroCredentialError) as raised:
                    parse_kiro_credentials(raw)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn(PROFILE, str(raised.exception))

    def test_security_reader_is_service_scoped_read_only_and_shell_free(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, credential_json().encode(), b""
            )
        )
        reader = SecurityKiroTokenReader(runner=runner)

        result = reader.read()

        self.assertEqual(result, credential_json())
        runner.assert_called_once_with(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KIRO_SOCIAL_SERVICE,
                "-w",
            ],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

    def test_security_reader_handles_missing_invalid_utf8_timeout_and_denial_safely(self):
        missing = SecurityKiroTokenReader(
            runner=Mock(return_value=subprocess.CompletedProcess([], 44, b"", b"missing"))
        )
        self.assertIsNone(missing.read())

        runners = (
            Mock(return_value=subprocess.CompletedProcess([], 0, b"\xff", b"")),
            Mock(return_value=subprocess.CompletedProcess([], 1, b"", SECRET.encode())),
            Mock(side_effect=subprocess.TimeoutExpired(["security"], 5, output=SECRET)),
        )
        for runner in runners:
            with self.subTest(runner=runner):
                with self.assertRaises(KeychainError) as raised:
                    SecurityKiroTokenReader(runner=runner).read()
                self.assertNotIn(SECRET, str(raised.exception))

    def test_default_security_reader_bounds_output_and_reaps_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "descendant.pid"
            helper = root / "security-helper"
            helper.write_text(
                f"#!{sys.executable}\n"
                "import os,sys,time\n"
                "child=os.fork()\n"
                "if child==0: time.sleep(30); raise SystemExit\n"
                f"open({str(pidfile)!r},'w').write(str(child))\n"
                "sys.stdout.buffer.write(b'x'*70000);sys.stdout.flush();time.sleep(30)\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            with self.assertRaises(KeychainError) as raised:
                SecurityKiroTokenReader(security_executable=str(helper)).read()
            self.assertNotIn(str(helper), str(raised.exception))
            descendant = int(pidfile.read_text(encoding="utf-8"))
            state = ""
            for _ in range(20):
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(descendant)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                if not state:
                    break
                time.sleep(0.05)
            self.assertEqual(state, "")


class KiroQuotaParserTests(unittest.TestCase):
    def test_parses_regular_credit_quota_and_row_reset(self):
        card = parse_kiro_quota(quota_payload(), NOW)

        self.assertEqual(card.provider_id, "kiro_cli")
        self.assertEqual(card.primary, "87.5 / 100 credits remaining")
        self.assertEqual(card.remaining_percent, 87.5)
        self.assertEqual(card.detail, "KIRO PRO · 12.5 used")
        self.assertEqual(card.source, "Kiro subscription quota")
        self.assertEqual(card.family_id, "kiro_cli")
        self.assertEqual(card.credential_source, "kiro_codewhisperer_api")
        self.assertEqual(card.source_kind, "official_api")
        self.assertEqual(card.resets_at, datetime.fromtimestamp(RESET, tz=timezone.utc))

    def test_precision_fields_win_and_numbers_use_at_most_two_decimals(self):
        payload = quota_payload(
            currentUsage=99,
            usageLimit=999,
            currentUsageWithPrecision=1.234,
            usageLimitWithPrecision=10.0,
        )

        card = parse_kiro_quota(payload, NOW)

        self.assertEqual(card.primary, "8.77 / 10 credits remaining")
        self.assertEqual(card.detail, "KIRO PRO · 1.23 used")
        self.assertAlmostEqual(card.remaining_percent, 87.66)

    def test_legacy_fields_and_response_reset_are_supported(self):
        payload = quota_payload(
            currentUsageWithPrecision=None,
            usageLimitWithPrecision=None,
            currentUsage=1,
            usageLimit=5,
            nextDateReset=None,
        )

        card = parse_kiro_quota(payload, NOW)

        self.assertEqual(card.primary, "4 / 5 credits remaining")
        self.assertEqual(
            card.resets_at, datetime.fromtimestamp(RESET + 10, tz=timezone.utc)
        )

    def test_active_bonus_is_context_but_inactive_bonus_is_ignored(self):
        active = quota_payload(
            freeTrialInfo={
                "freeTrialStatus": "ACTIVE",
                "currentUsageWithPrecision": 2,
                "usageLimitWithPrecision": 10,
            }
        )
        inactive = quota_payload(
            freeTrialInfo={
                "freeTrialStatus": "EXPIRED",
                "currentUsageWithPrecision": 2,
                "usageLimitWithPrecision": 10,
            }
        )

        self.assertIn("Bonus 8 / 10 remaining", parse_kiro_quota(active, NOW).detail)
        self.assertNotIn("Bonus", parse_kiro_quota(inactive, NOW).detail)

    def test_used_is_clamped_to_quota_bounds(self):
        self.assertEqual(
            parse_kiro_quota(quota_payload(currentUsageWithPrecision=150), NOW).remaining_percent,
            0,
        )
        self.assertEqual(
            parse_kiro_quota(quota_payload(currentUsageWithPrecision=-5), NOW).remaining_percent,
            100,
        )

    def test_rejects_missing_zero_boolean_or_nonfinite_limits(self):
        for limit in (None, 0, True, float("nan"), float("inf")):
            with self.subTest(limit=limit):
                with self.assertRaises(KiroParseError):
                    parse_kiro_quota(
                        quota_payload(usageLimitWithPrecision=limit, usageLimit=None), NOW
                    )

    def test_ignores_invalid_reset_timestamp(self):
        card = parse_kiro_quota(
            quota_payload(nextDateReset="not-a-time")
            | {"nextDateReset": float("nan")},
            NOW,
        )
        self.assertIsNone(card.resets_at)


class FakeTokenReader:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def read(self):
        if self.error:
            raise self.error
        return self.value


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def get_json(self, endpoint, headers):
        self.calls.append((endpoint, headers))
        if self.error:
            raise self.error
        return self.payload


class KiroQuotaAdapterTests(unittest.TestCase):
    def adapter(self, *, raw=credential_json(), payload=None, error=None, reader_error=None):
        client = FakeClient(payload=payload or quota_payload(), error=error)
        adapter = KiroQuotaAdapter(
            client=client,
            clock=lambda: NOW,
            token_reader=FakeTokenReader(raw, reader_error),
        )
        return adapter, client

    def test_fetch_builds_fixed_aws_request_and_returns_quota(self):
        adapter, client = self.adapter()

        overview = adapter.fetch()

        self.assertEqual(overview.cards[0].source, "Kiro subscription quota")
        endpoint, headers = client.calls[0]
        self.assertTrue(endpoint.startswith("https://q.us-east-1.amazonaws.com/getUsageLimits?"))
        self.assertIn("origin=AI_EDITOR", endpoint)
        self.assertIn("resourceType=AGENTIC_REQUEST", endpoint)
        self.assertIn("profileArn=arn%3Aaws%3Acodewhisperer", endpoint)
        self.assertEqual(headers["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(headers["User-Agent"], "KiroIDE")

    def test_request_id_uses_packaged_os_randomness_without_uuid_dependency(self):
        credentials = parse_kiro_credentials(credential_json())
        with patch(
            "openusage_bar.kiro.os.urandom",
            return_value=bytes.fromhex("00112233445566778899aabbccddeeff"),
        ):
            headers = KiroQuotaAdapter._headers(credentials)

        self.assertEqual(
            headers["amz-sdk-invocation-id"],
            "00112233-4455-6677-8899-aabbccddeeff",
        )

    def test_default_client_allows_proxy_dns_only_for_validated_aws_host(self):
        client = FakeClient(payload=quota_payload())
        with patch("openusage_bar.kiro.BoundedHTTPClient", return_value=client) as client_type:
            adapter = KiroQuotaAdapter(
                clock=lambda: NOW,
                token_reader=FakeTokenReader(credential_json()),
            )
            overview = adapter.fetch()

        self.assertEqual(overview.cards[0].provider_id, "kiro_cli")
        client_type.assert_called_once_with(
            allowed_reserved_hosts={"q.us-east-1.amazonaws.com"},
            allowed_redirect_hosts={"q.us-east-1.amazonaws.com"},
        )

    def test_missing_credentials_returns_no_override_without_network(self):
        adapter, client = self.adapter(raw=None)

        self.assertEqual(adapter.fetch().cards, [])
        self.assertEqual(client.calls, [])

    def test_failures_are_sanitized_and_return_no_override(self):
        cases = (
            ("keychain", {"reader_error": KeychainError("denied " + SECRET)}),
            ("credential", {"raw": "invalid " + SECRET}),
            ("authentication", {"error": AuthenticationRequired("payload " + SECRET)}),
            ("rate limit", {"error": RateLimited("payload " + SECRET)}),
            ("network", {"error": NetworkError("payload " + SECRET)}),
            ("response", {"payload": {"secret": SECRET}}),
        )
        for expected, kwargs in cases:
            with self.subTest(expected=expected):
                adapter, _client = self.adapter(**kwargs)
                with self.assertLogs("openusage_bar.kiro", level="WARNING") as logs:
                    overview = adapter.fetch()
                output = "\n".join(logs.output)
                self.assertEqual(overview.cards, [])
                self.assertIn(expected, output)
                self.assertNotIn(SECRET, output)
                self.assertNotIn(PROFILE, output)


if __name__ == "__main__":
    unittest.main()
