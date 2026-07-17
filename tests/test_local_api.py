from __future__ import annotations

import http.client
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.capabilities import (
    CapabilityState,
    OperatingSystem,
    ProviderRegistry,
    QuotaWindow,
    QuotaWindowCapability,
    SourceProvenance,
    SourceStability,
    registry,
)
from openusage_bar.local_api import create_tcp_server, create_unix_server
from openusage_bar.collector_cli import main as collector_main
from openusage_bar.provider_catalog import catalog
from openusage_bar.query import QueryService


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
TOKEN = "t" * 48


def seeded_query() -> tuple[ActivityStore, QueryService]:
    store = ActivityStore(":memory:")
    store.replace_daily_usage("codex", "2026-07-14", [DailyUsageRow(
        day="2026-07-14", provider_id="codex", model_id="gpt-5.5",
        input_tokens=60, output_tokens=20, cache_read_tokens=20,
        cache_creation_tokens=0, reasoning_tokens=None, total_tokens=100,
        cost_amount=None, cost_currency=None, cost_basis=None, quality="direct",
        imported_at="2026-07-14T09:00:00Z",
    )])
    store.replace_daily_costs("openai", "2026-07-14", [DailyCostRow(
        day="2026-07-14", provider_id="openai", cost_kind="actual",
        currency="USD", amount="12.34", basis="provider_reported",
        quality="direct", imported_at="2026-07-14T09:00:00Z",
    )])
    store.record_quota(QuotaObservation(
        record_id="minimax.five_hour", observed_at="2026-07-14T09:00:00Z",
        provider_id="minimax", quota_name="Five hour", unit="percent",
        used="82", quota_limit="100", remaining="18", remaining_ratio=0.18,
        resets_at="2026-07-14T12:00:00Z", period_start=None, period_end=None,
        state="ok", quality="direct", stale=False,
    ))
    store.record_source_success("minimax", "current.quota", NOW)
    store.upsert_provider_instance(ProviderInstance(
        provider_id="minimax-primary", family_id="minimax",
        display_name="MiniMax primary", category="subscription",
        credential_source="minimax_builtin_api", source_kind="builtin_api",
        observed_at="2026-07-14T09:00:00Z",
    ))
    store.upsert_provider_instance(ProviderInstance(
        provider_id="zfuture", family_id="zfuture",
        display_name="Future Provider", category="api",
        credential_source="openusage", source_kind="openusage",
        observed_at="2026-07-14T09:01:00Z",
    ))
    return store, QueryService(store, clock=lambda: NOW)


def start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _capture_error(action, errors):
    try:
        action()
    except Exception as error:
        errors.append(error)


def unix_request(path: Path, target: str, *, method: str = "GET", headers=None, body=b""):
    headers = {"Host": "localhost", **(headers or {})}
    lines = [f"{method} {target} HTTP/1.1", *(f"{k}: {v}" for k, v in headers.items()), "", ""]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(path))
    client.sendall("\r\n".join(lines).encode("ascii") + body)
    response = http.client.HTTPResponse(client)
    response._method = method
    response.begin()
    payload = response.read()
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, payload
    client.close()
    return result


def unix_raw_request(path: Path, request: bytes):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(path))
    client.sendall(request)
    response = http.client.HTTPResponse(client)
    response.begin()
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    client.close()
    return result


def raw_exchange(address, request: bytes) -> bytes:
    if isinstance(address, (str, Path)):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(address))
    else:
        client = socket.create_connection(address, timeout=2)
    client.settimeout(2)
    try:
        client.sendall(request)
        chunks = []
        while True:
            try:
                chunk = client.recv(65_536)
            except (ConnectionResetError, TimeoutError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client.close()


def split_raw_response(response: bytes):
    head, body = response.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
    return lines[0], headers, body


class UnixLocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "private"
        self.socket_path = self.root / "api.sock"
        self.store, self.query = seeded_query()
        self.server = create_unix_server(self.socket_path, self.query, clock=lambda: NOW)
        self.thread = start(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def request(self, target, **kwargs):
        return unix_request(self.socket_path, target, **kwargs)

    def test_default_unix_socket_and_parent_are_user_only_and_cleaned(self):
        self.assertTrue(stat.S_ISSOCK(self.socket_path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.socket_path.exists())

    def test_routes_reuse_canonical_query_envelopes(self):
        expectations = {
            "/v1/summary?today=2026-07-14": "todayTokens",
            "/v1/snapshot?today=2026-07-14": "quotaWindows",
            "/v1/capacity": "providers",
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14": "rows",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14": "rows",
            "/v1/quotas/history?limit=10": "snapshots",
            "/v1/sources/status": "sources",
            "/v1/changes?after=0&limit=10": "records",
            "/v1/capabilities": "providers",
            "/v1/providers": "providers",
            "/v1/health": "health",
            "/v1/schema": "routes",
            "/schema": "routes",
        }
        for target, field in expectations.items():
            with self.subTest(target=target):
                status, headers, body = self.request(target)
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["schemaVersion"], "1.0")
                self.assertIn("dataRevision", payload)
                self.assertIn(field, payload)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertNotIn("access-control-allow-origin", headers)

    def test_snapshot_api_and_cli_are_exactly_identical(self):
        status, _, body = self.request("/v1/snapshot?today=2026-07-14")
        stdout, stderr = io.StringIO(), io.StringIO()
        code = collector_main(
            [
                "snapshot", "--today", "2026-07-14",
                "--format", "json", "--offline",
            ],
            stdout=stdout,
            stderr=stderr,
            store=self.store,
            query=self.query,
            clock=lambda: NOW,
        )

        self.assertEqual((status, code, stderr.getvalue()), (200, 0, ""))
        self.assertEqual(json.loads(body), json.loads(stdout.getvalue()))

    def test_costs_route_filters_provider_and_currency_and_rejects_bad_parameters(self):
        status, _, body = self.request(
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&providerIds=openai&currencies=USD"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["rows"][0]["amount"], "12.34")
        self.assertTrue(payload["coverage"][0]["covered"])

        for target in (
            "/v1/costs/daily?from=2026-07-14",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&unknown=1",
            "/v1/costs/daily?from=2026-07-14&from=2026-07-13&to=2026-07-14",
            "/v1/costs/daily?from=bad&to=2026-07-14",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&currencies=usd",
        ):
            with self.subTest(target=target):
                status, _, body = self.request(target)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_capabilities_are_complete_canonical_nonlocalized_family_contract(self):
        status, _, body = self.request("/v1/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["providers"]), 37)
        self.assertEqual(
            [row["familyId"] for row in payload["providers"]],
            sorted(row["familyId"] for row in payload["providers"]),
        )
        self.assertEqual(payload["upstream"], {
            "name": "openusage", "version": "0.23.0", "revision": "3059f1b",
            "familyCount": 35,
        })
        for family in payload["providers"]:
            self.assertEqual(set(family), {
                "providerId", "familyId", "displayName", "category",
                "metricFamilies",
                "regions", "supportsAccounts", "capabilities", "sources",
            })
            self.assertEqual(family["providerId"], family["familyId"])
            self.assertTrue(family["metricFamilies"])
            self.assertEqual(set(family["capabilities"]), {
                "quotaWindows", "tokenHistory", "modelBreakdown",
                "resetTimestamps", "billing", "credits", "balance", "cost",
                "rateLimits", "serviceStatus",
            })
            self.assertEqual(
                set(family["capabilities"]["quotaWindows"]),
                {"state", "values"},
            )
            for source in family["sources"]:
                self.assertEqual(set(source), {
                    "sourceId", "kind", "timeoutSeconds", "freshnessSeconds",
                    "credentialType", "requiresCredential", "operatingSystems",
                    "stability", "provenance",
                })
        lowered = json.dumps(payload, ensure_ascii=False).lower()
        for localized in ("正常", "错误", "未配置", "过期"):
            self.assertNotIn(localized, lowered)

    def test_capabilities_expose_known_facts_and_preserve_unknown_semantics(self):
        _, _, body = self.request("/v1/capabilities")
        providers = {
            provider["familyId"]: provider
            for provider in json.loads(body)["providers"]
        }

        self.assertEqual(providers["codex"]["capabilities"], {
            "quotaWindows": {
                "state": "supported", "values": ["five_hour", "weekly"],
            },
            "tokenHistory": "supported",
            "modelBreakdown": "supported",
            "resetTimestamps": "supported",
            "billing": "unknown",
            "credits": "unknown",
            "balance": "unknown",
            "cost": "unknown",
            "rateLimits": "unknown",
            "serviceStatus": "unknown",
        })
        self.assertEqual(
            providers["kiro_cli"]["capabilities"]["quotaWindows"],
            {"state": "supported", "values": ["billing_cycle"]},
        )
        self.assertEqual(
            providers["kiro_cli"]["capabilities"]["credits"], "supported"
        )
        self.assertEqual(
            providers["step_plan"]["capabilities"]["billing"], "supported"
        )
        self.assertEqual(
            providers["step_plan"]["sources"][0], {
                "sourceId": "step_plan_browser_session",
                "kind": "browser_session",
                "timeoutSeconds": 12,
                "freshnessSeconds": 300,
                "credentialType": "browser_session",
                "requiresCredential": True,
                "operatingSystems": ["macos"],
                "stability": "experimental",
                "provenance": "user_session",
            },
        )
        openusage = providers["codex"]["sources"][1]
        self.assertEqual(
            (openusage["sourceId"], openusage["operatingSystems"],
             openusage["stability"], openusage["provenance"]),
            ("openusage", ["macos"], "pinned", "openusage_upstream"),
        )

        self.assertEqual(
            providers["openai"]["capabilities"]["quotaWindows"],
            {"state": "unknown", "values": []},
        )
        self.assertNotEqual(
            providers["openai"]["capabilities"]["quotaWindows"]["state"],
            "unsupported",
        )
        for provider in providers.values():
            quota = provider["capabilities"]["quotaWindows"]
            if quota["state"] == "supported":
                self.assertTrue(quota["values"])
            else:
                self.assertIn(quota["state"], {"unknown", "unsupported"})
                self.assertEqual(quota["values"], [])

    def test_capabilities_do_not_expose_private_credential_or_account_fields(self):
        _, _, body = self.request("/v1/capabilities")
        payload = json.loads(body)
        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(payload)
        self.assertIn("tokenHistory", keys)
        self.assertTrue({
            "credentialScope", "credentialScopes", "path", "paths", "token",
            "tokens", "account", "accounts", "accountRef", "raw", "rawPayload",
        }.isdisjoint(keys))

    def test_weak_etag_ignores_only_generated_at(self):
        status, headers, body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        self.assertTrue(headers["etag"].startswith('W/"'))
        first = json.loads(body)

        self.query.clock = lambda: NOW + timedelta(seconds=30)
        status, advanced_headers, advanced_body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        advanced = json.loads(advanced_body)
        self.assertNotEqual(first["generatedAt"], advanced["generatedAt"])
        self.assertEqual(headers["etag"], advanced_headers["etag"])
        first.pop("generatedAt")
        advanced.pop("generatedAt")
        self.assertEqual(first, advanced)

        status, cached_headers, cached_body = self.request(
            "/v1/providers", headers={"If-None-Match": headers["etag"]}
        )
        self.assertEqual((status, cached_body), (304, b""))
        self.assertEqual(cached_headers["etag"], headers["etag"])

    def test_capability_etag_changes_for_every_capability_and_source_metadata_mutation(self):
        _, baseline_headers, _ = self.request("/v1/capabilities")
        baseline = baseline_headers["etag"]
        first = registry.descriptors[0]
        mutations = [(
            "quota_windows",
            replace(
                first,
                capabilities=replace(
                    first.capabilities,
                    quota_windows=QuotaWindowCapability(
                        CapabilityState.SUPPORTED, (QuotaWindow.MONTHLY,)
                    ),
                ),
            ),
        )]
        for field in (
            "token_history", "model_breakdown", "reset_timestamps", "billing",
            "credits", "balance", "cost", "rate_limits", "service_status",
        ):
            current = getattr(first.capabilities, field)
            changed = (
                CapabilityState.UNKNOWN
                if current is not CapabilityState.UNKNOWN
                else CapabilityState.SUPPORTED
            )
            mutations.append((
                field,
                replace(
                    first,
                    capabilities=replace(first.capabilities, **{field: changed}),
                ),
            ))
        source_mutations = {
            "operating_systems": frozenset({
                *first.sources[0].operating_systems, OperatingSystem.LINUX,
            }),
            "stability": SourceStability.STABLE,
            "provenance": SourceProvenance.PROVIDER_OFFICIAL,
        }
        for field, changed in source_mutations.items():
            mutations.append((
                field,
                replace(
                    first,
                    sources=(
                        replace(first.sources[0], **{field: changed}),
                        *first.sources[1:],
                    ),
                ),
            ))

        for label, descriptor in mutations:
            with self.subTest(mutation=label):
                self.server.router.provider_registry = ProviderRegistry(
                    (descriptor, *registry.descriptors[1:])
                )
                _, headers, _ = self.request("/v1/capabilities")
                self.assertNotEqual(headers["etag"], baseline)

        self.server.router.provider_registry = registry
        with patch(
            "openusage_bar.local_api.default_catalog",
            replace(catalog, upstream_revision="updated-revision"),
        ):
            _, headers, _ = self.request("/v1/capabilities")
        self.assertNotEqual(headers["etag"], baseline)

    def test_provider_instance_semantic_change_changes_etag(self):
        _, baseline_headers, _ = self.request("/v1/providers")
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-primary", family_id="minimax",
            display_name="MiniMax corrected", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:05:00Z",
        ))
        _, changed_headers, _ = self.request("/v1/providers")
        self.assertNotEqual(changed_headers["etag"], baseline_headers["etag"])

    def test_provider_instances_filter_head_etag_and_privacy_contract(self):
        status, headers, body = self.request("/v1/providers?providerIds=minimax-primary")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["providers"]), 1)
        self.assertEqual(set(payload["providers"][0]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })
        serialized = json.dumps(payload).lower()
        for forbidden in (
            "email", "account", "path", "token", "cookie", "payloadhash",
            "payload_hash", "change_seq", "record_type", "raw", "attributes",
        ):
            self.assertNotIn(forbidden, serialized)
        head_status, head_headers, head_body = self.request(
            "/v1/providers?providerIds=minimax-primary", method="HEAD"
        )
        self.assertEqual(head_status, 200)
        self.assertEqual(head_headers["etag"], headers["etag"])
        self.assertEqual(head_headers["content-length"], headers["content-length"])
        self.assertEqual(head_body, b"")
        cached_status, _, cached_body = self.request(
            "/v1/providers?providerIds=minimax-primary",
            headers={"If-None-Match": headers["etag"]},
        )
        self.assertEqual((cached_status, cached_body), (304, b""))

    def test_provider_instances_use_catalog_brand_without_changing_identity(self):
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-generated", family_id="minimax",
            display_name="minimax", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="mistral-custom", family_id="mistral",
            display_name="miſtral", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:01:00Z",
        ))

        status, _, body = self.request(
            "/v1/providers?providerIds=minimax-generated,mistral-custom"
        )
        providers = {
            item["providerId"]: item for item in json.loads(body)["providers"]
        }
        provider = providers["minimax-generated"]

        self.assertEqual(status, 200)
        self.assertEqual(provider["providerId"], "minimax-generated")
        self.assertEqual(provider["familyId"], "minimax")
        self.assertEqual(provider["displayName"], "MiniMax")
        self.assertEqual(providers["mistral-custom"]["displayName"], "miſtral")

    def test_provider_id_set_semantics_canonicalize_body_and_etag(self):
        targets = (
            "/v1/providers?providerIds=minimax-primary,zfuture",
            "/v1/providers?providerIds=zfuture,minimax-primary",
            "/v1/providers?providerIds=zfuture,minimax-primary,zfuture",
        )
        responses = [self.request(target) for target in targets]
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        self.assertEqual(len({body for _, _, body in responses}), 1)
        self.assertEqual(len({headers["etag"] for _, headers, _ in responses}), 1)

        etag = responses[0][1]["etag"]
        status, head_headers, body = self.request(targets[1], method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(head_headers["etag"], etag)
        status, headers, body = self.request(
            targets[2], headers={"If-None-Match": etag}
        )
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers["etag"], etag)

        unfiltered = self.request("/v1/providers")
        explicit_empty = self.request("/v1/providers?providerIds=")
        self.assertEqual(unfiltered[1]["etag"], explicit_empty[1]["etag"])
        self.assertEqual(unfiltered[2], explicit_empty[2])

    def test_provider_instances_reject_invalid_id_without_echo(self):
        secret = "bad%2FSECRET"
        status, _, body = self.request(f"/v1/providers?providerIds={secret}")
        self.assertEqual(status, 400)
        self.assertNotIn(secret.encode(), body)

    def test_schema_includes_provider_route(self):
        status, _, body = self.request("/v1/schema")
        self.assertEqual(status, 200)
        self.assertIn("/v1/providers", json.loads(body)["routes"])

    def test_head_has_get_headers_without_a_body(self):
        get_status, get_headers, _ = self.request("/v1/capacity")
        status, headers, body = self.request("/v1/capacity", method="HEAD")
        self.assertEqual((get_status, status), (200, 200))
        self.assertEqual(headers["etag"], get_headers["etag"])
        self.assertEqual(headers["content-length"], get_headers["content-length"])
        self.assertEqual(body, b"")

    def test_etag_is_stable_and_if_none_match_returns_empty_304(self):
        status, headers, body = self.request("/v1/summary?today=2026-07-14")
        self.assertEqual(status, 200)
        self.assertTrue(body)
        status, cached_headers, body = self.request(
            "/v1/summary?today=2026-07-14", headers={"If-None-Match": headers["etag"]}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(cached_headers["etag"], headers["etag"])
        self.assertNotIn("content-length", cached_headers)

    def test_if_none_match_uses_weak_list_and_wildcard_semantics_for_get_and_head(self):
        target = "/v1/providers"
        _, headers, _ = self.request(target)
        weak = headers["etag"]
        strong = weak[2:]
        validators = (strong, f'"un,related", {weak}', "*")
        for method in ("GET", "HEAD"):
            for validator in validators:
                with self.subTest(method=method, validator=validator):
                    status, cached_headers, body = self.request(
                        target,
                        method=method,
                        headers={"If-None-Match": validator},
                    )
                    self.assertEqual((status, body), (304, b""))
                    self.assertEqual(cached_headers["etag"], weak)
                    self.assertNotIn("content-length", cached_headers)

        status, _, body = self.request(
            target, headers={"If-None-Match": '"different"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_invalid_or_duplicate_if_none_match_is_sanitized(self):
        for invalid in ('W/ "broken"', '*, "other"', '"unterminated'):
            with self.subTest(invalid=invalid):
                status, _, body = self.request(
                    "/v1/providers", headers={"If-None-Match": invalid}
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(body)["error"]["code"], "invalid_header"
                )
                self.assertNotIn(invalid.encode(), body)

        response = raw_exchange(
            self.socket_path,
            b"GET /v1/providers HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"If-None-Match: \"one\"\r\n"
            b"If-None-Match: \"two\"\r\n\r\n",
        )
        status, _, body = split_raw_response(response)
        self.assertTrue(status.startswith(b"HTTP/1.1 400 "))
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")

    def test_origin_is_absent_or_exactly_allowlisted(self):
        status, _, _ = self.request("/v1/health", headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_methods_and_bodies_are_rejected_without_route_execution(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = self.request("/v1/health", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, HEAD")
                self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")
        status, _, body = self.request(
            "/v1/health", headers={"Content-Length": "1"}, body=b"x"
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "request_body_not_allowed")
        status, _, body = self.request(
            "/v1/health", method="POST", headers={"Content-Length": "1"}, body=b"x"
        )
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")

    def test_strict_boundary_validation_has_stable_sanitized_errors(self):
        targets = (
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14&unknown=1",
            "/v1/activity/daily?from=2026-07-14&from=2026-07-13&to=2026-07-14",
            "/v1/activity/daily?from=2026-01-01&to=2028-12-31",
            "/v1/activity/daily?from=bad&to=2026-07-14",
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14&providerIds=bad/id",
            "/v1/capacity?limit=1001",
            "/v1/changes?after=-1",
            "/v1/changes?after=999999999999999999999999999999999999",
            "/v1/changes?after=999",
            "/v1/changes?after=%ZZ",
            "/v1/health?x=%00",
            "/v1/quotas/history?providerId=",
            "/v1/quotas/history?accountRef=",
        )
        for target in targets:
            with self.subTest(target=target):
                status, headers, body = self.request(target)
                self.assertEqual(status, 400)
                payload = json.loads(body)
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(set(payload["error"]), {"code", "message"})
                self.assertNotIn(self.temp.name, body.decode())
                self.assertEqual(headers["cache-control"], "no-store")

    def test_request_line_header_count_and_parser_errors_are_bounded_json(self):
        requests = (
            b"GET /" + (b"a" * 8_192) + b" HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n" +
            b"".join(f"X-{index}: x\r\n".encode("ascii") for index in range(110)) + b"\r\n",
            b"GET /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:30]):
                status, headers, body = unix_raw_request(self.socket_path, request)
                self.assertIn(status, {400, 413})
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertIn(json.loads(body)["error"]["code"], {"invalid_request", "request_too_large"})

    def test_parser_level_head_errors_have_representation_length_without_wire_body(self):
        oversized_headers = b"".join(
            f"X-{index}: x\r\n".encode("ascii") for index in range(110)
        )
        requests = (
            b"HEAD /v1/health HTTP/1.1\r\nHost: localhost\r\n" + oversized_headers + b"\r\n",
            b"HEAD /v1/health HTTP/1.1\r\nHost: localhost\r\nX-Large: " + b"x" * 65_537 + b"\r\n\r\n",
            b"HEAD /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:50]):
                status, headers, body = split_raw_response(raw_exchange(self.socket_path, request))
                self.assertTrue(status.startswith((b"HTTP/1.1 400 ", b"HTTP/1.1 413 ")))
                self.assertGreater(int(headers["content-length"]), 0)
                self.assertEqual(body, b"")

        status, headers, body = split_raw_response(raw_exchange(
            self.socket_path,
            b"GET /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        ))
        self.assertTrue(status.startswith(b"HTTP/1.1 400 "))
        self.assertEqual(len(body), int(headers["content-length"]))
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

    def test_oversized_request_line_preserves_strict_head_wire_semantics(self):
        target = b"/" + b"a" * 8_192
        for method, expects_body in ((b"HEAD", False), (b"GET", True), (b"HEADX", True)):
            with self.subTest(method=method):
                response = raw_exchange(
                    self.socket_path,
                    method + b" " + target + b" HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )
                status, headers, body = split_raw_response(response)
                self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
                representation_length = int(headers["content-length"])
                self.assertGreater(representation_length, 0)
                if expects_body:
                    self.assertEqual(len(body), representation_length)
                    self.assertEqual(json.loads(body)["error"]["code"], "request_too_large")
                else:
                    self.assertEqual(body, b"")

    def test_http_09_10_absolute_form_and_obs_fold_are_rejected_and_closed(self):
        requests = (
            b"GET /v1/health\r\n",
            b"GET /v1/health HTTP/1.0\r\nHost: localhost\r\n\r\n",
            b"GET http://localhost/v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nX-Test: a\r\n folded\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:40]):
                response = raw_exchange(str(self.socket_path), request)
                self.assertTrue(response.startswith(b"HTTP/1.1 400 "), response[:100])
                self.assertIn(b"Connection: close\r\n", response)
                self.assertEqual(response.count(b"HTTP/1.1 400 "), 1)
                self.assertNotIn(b"HTTP/1.1 200 ", response)

    def test_unknown_route_and_internal_failure_are_sanitized(self):
        status, _, body = self.request("/v1/missing")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "not_found"))
        original = self.query.capacity
        self.query.capacity = lambda *_: (_ for _ in ()).throw(RuntimeError("secret /tmp/sql"))
        try:
            status, _, body = self.request("/v1/capacity")
        finally:
            self.query.capacity = original
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")
        self.assertNotIn("secret", body.decode())

    def test_concurrent_reads_complete(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.request("/v1/capacity")[0])) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(results, [200] * 12)


class SocketLifecycleTests(unittest.TestCase):
    def test_symlink_regular_file_and_live_socket_are_never_clobbered(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            try:
                root = Path(temporary)
                target = root / "target"
                target.write_text("keep", encoding="utf-8")
                link = root / "api.sock"
                link.symlink_to(target)
                with self.assertRaises(OSError):
                    create_unix_server(link, query)
                link.unlink()
                link.write_text("keep", encoding="utf-8")
                with self.assertRaises(OSError):
                    create_unix_server(link, query)
                link.unlink()
                live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                live.bind(str(link))
                live.listen(1)
                try:
                    with self.assertRaises(OSError):
                        create_unix_server(link, query)
                finally:
                    live.close()
                    link.unlink()
            finally:
                store.close()

    def test_stale_socket_is_replaced_but_replacement_after_start_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "api.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            server = create_unix_server(path, query)
            server.socket.close()
            path.unlink()
            path.write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "restored an unexpected replacement"):
                server.server_close()
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement")
            store.close()

    def test_cleanup_quarantine_preserves_file_and_socket_replacements(self):
        for replacement in ("file", "socket"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                store, query = seeded_query()
                path = Path(temporary) / "private" / "api.sock"
                held = []

                def replace(original):
                    if replacement == "file":
                        original.write_text("replacement", encoding="utf-8")
                    else:
                        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        peer.bind(str(original))
                        peer.listen(1)
                        held.append(peer)

                server = create_unix_server(path, query, cleanup_hook=replace)
                server.server_close()
                try:
                    if replacement == "file":
                        self.assertEqual(path.read_text(encoding="utf-8"), "replacement")
                    else:
                        self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
                finally:
                    for peer in held:
                        peer.close()
                    path.unlink(missing_ok=True)
                    store.close()


class DeadlineTests(unittest.TestCase):
    def test_absolute_deadline_evicts_slow_drip_and_releases_only_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(
                path, query, max_threads=1, client_timeout=1,
                request_deadline=0.12,
            )
            thread = start(server)
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.connect(str(path))

            def drip():
                for byte in b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n":
                    try:
                        slow.send(bytes([byte]))
                    except OSError:
                        return
                    time.sleep(0.04)

            dripper = threading.Thread(target=drip)
            dripper.start()
            time.sleep(0.25)
            status, _, _ = unix_request(path, "/v1/health")
            self.assertEqual(status, 200)
            dripper.join(1)
            slow.close()
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertEqual(server.active_deadline_count, 0)
            store.close()

    def test_shutdown_cancels_deadline_and_interrupts_active_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(path, query, max_threads=1, request_deadline=10)
            thread = start(server)
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.connect(str(path))
            slow.sendall(b"G")
            deadline = time.monotonic() + 1
            while server.active_deadline_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server.active_deadline_count, 1)
            server.shutdown()
            server.server_close()
            thread.join(2)
            slow.close()
            self.assertEqual(server.active_deadline_count, 0)
            store.close()

    def test_close_cannot_join_registered_timer_before_it_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(
                path, query, max_threads=1, request_deadline=0.2,
            )
            thread = start(server)
            registered = threading.Event()
            allow_start = threading.Event()
            expired = threading.Event()
            original_start = threading.Timer.start
            original_expire = server._expire_request

            def mark_timer_expiry(request):
                if isinstance(threading.current_thread(), threading.Timer):
                    expired.set()
                return original_expire(request)

            server._expire_request = mark_timer_expiry

            def blocked_start(timer):
                registered.set()
                self.assertTrue(allow_start.wait(2))
                return original_start(timer)

            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with patch("openusage_bar.local_api.threading.Timer.start", blocked_start):
                slow.connect(str(path))
                slow.sendall(b"G")
                self.assertTrue(registered.wait(1))
                server.shutdown()
                close_errors = []
                closer = threading.Thread(
                    target=lambda: _capture_error(server.server_close, close_errors)
                )
                closer.start()
                time.sleep(0.05)
                allow_start.set()
                closer.join(2)

            slow.close()
            thread.join(2)
            time.sleep(0.3)
            self.assertEqual(close_errors, [])
            self.assertFalse(closer.is_alive())
            self.assertEqual(server.active_deadline_count, 0)
            self.assertFalse(path.exists())
            self.assertFalse(expired.is_set())
            store.close()


class TCPLocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store, self.query = seeded_query()
        self.token_path = Path(self.temp.name) / "api.token"
        self.server = create_tcp_server(
            self.query, port=0, bearer_token=TOKEN, token_path=self.token_path,
            allowed_origins={"https://scheduler.local"}, clock=lambda: NOW,
        )
        self.thread = start(self.server)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def request(self, target="/v1/health", *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        base = {"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}"}
        base.update(headers or {})
        connection.request(method, target, headers=base)
        response = connection.getresponse()
        result = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        connection.close()
        return result

    def test_tcp_is_loopback_only_and_token_file_is_0600(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), TOKEN)

    def test_provider_contract_matches_unix_transport_shape(self):
        status, _, body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["providers"][0]["familyId"], "minimax")
        self.assertEqual(set(payload["providers"][0]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })

    def test_tcp_provider_id_set_semantics_have_one_etag_and_body(self):
        targets = (
            "/v1/providers?providerIds=minimax-primary,zfuture",
            "/v1/providers?providerIds=zfuture,minimax-primary",
            "/v1/providers?providerIds=zfuture,minimax-primary,zfuture",
        )
        responses = [self.request(target) for target in targets]
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        self.assertEqual(len({body for _, _, body in responses}), 1)
        self.assertEqual(len({headers["etag"] for _, headers, _ in responses}), 1)
        self.assertTrue(responses[0][1]["etag"].startswith('W/"'))

    def test_tcp_if_none_match_weak_list_wildcard_and_invalid_semantics(self):
        target = "/v1/providers"
        _, headers, _ = self.request(target)
        weak = headers["etag"]
        for method in ("GET", "HEAD"):
            for validator in (weak[2:], f'"other", {weak}', "*"):
                with self.subTest(method=method, validator=validator):
                    status, response_headers, body = self.request(
                        target,
                        method=method,
                        headers={"If-None-Match": validator},
                    )
                    self.assertEqual((status, body), (304, b""))
                    self.assertEqual(response_headers["etag"], weak)

        status, _, body = self.request(
            target, headers={"If-None-Match": 'W/ "invalid"'}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")
        status, _, body = self.request(
            target, headers={"If-None-Match": '"different"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_tcp_requires_exact_bearer_without_leaking_it(self):
        for authorization in (None, "Bearer wrong", "Basic whatever"):
            headers = {"Authorization": authorization} if authorization else {"Authorization": ""}
            status, _, body = self.request(headers=headers)
            self.assertEqual(status, 401)
            self.assertNotIn(TOKEN.encode(), body)

    def test_tcp_parser_level_head_error_has_no_wire_body(self):
        request = (
            b"HEAD /v1/health HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode("ascii")
            + b"".join(f"X-{index}: x\r\n".encode("ascii") for index in range(110))
            + b"\r\n"
        )
        status, headers, body = split_raw_response(
            raw_exchange(("127.0.0.1", self.port), request)
        )
        self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
        self.assertGreater(int(headers["content-length"]), 0)
        self.assertEqual(body, b"")

        target = b"/" + b"a" * 8_192
        for method, expects_body in ((b"HEAD", False), (b"GET", True)):
            with self.subTest(oversized_method=method):
                oversized = (
                    method + b" " + target + b" HTTP/1.1\r\n"
                    + f"Host: 127.0.0.1:{self.port}\r\n\r\n".encode("ascii")
                )
                status, headers, body = split_raw_response(
                    raw_exchange(("127.0.0.1", self.port), oversized)
                )
                self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
                representation_length = int(headers["content-length"])
                self.assertGreater(representation_length, 0)
                self.assertEqual(len(body), representation_length if expects_body else 0)

    def test_ambiguous_framing_and_duplicate_security_headers_close_before_tail_request(self):
        good = (
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n\r\n"
        ).encode("ascii")
        prefixes = (
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\nx",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: nope\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n0\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nHost: evil.test\r\nAuthorization: Bearer {TOKEN}\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nAuthorization: Bearer wrong\r\n\r\n",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix[:60]):
                response = raw_exchange(("127.0.0.1", self.port), prefix.encode("ascii") + good)
                self.assertTrue(
                    response.startswith(b"HTTP/1.1 400 ") or response.startswith(b"HTTP/1.1 413 "),
                    response[:100],
                )
                self.assertIn(b"Connection: close\r\n", response)
                self.assertEqual(response.count(b"HTTP/1.1 "), 1)
                self.assertNotIn(b"200 OK", response)

    def test_host_rejects_dns_rebinding_nonloopback_wrong_port_and_malformed_values(self):
        for host in ("evil.test", "127.0.0.1", "127.0.0.1:1", "localhost:%s" % self.port, "[::1]:%s" % self.port):
            with self.subTest(host=host):
                status, _, body = self.request(headers={"Host": host})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["error"]["code"], "forbidden_host")

    def test_allowlisted_origin_is_echoed_without_wildcard(self):
        status, headers, _ = self.request(headers={"Origin": "https://scheduler.local"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "https://scheduler.local")
        self.assertNotEqual(headers["access-control-allow-origin"], "*")
        status, _, _ = self.request(headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_low_entropy_token_is_rejected_and_generation_is_secure(self):
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0, bearer_token="short")
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0)
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0, bearer_token=TOKEN, allowed_origins={"*"})
        generated_path = Path(self.temp.name) / "generated.token"
        server = create_tcp_server(self.query, port=0, token_path=generated_path)
        try:
            self.assertGreaterEqual(len(server.bearer_token), 43)
            self.assertEqual(stat.S_IMODE(generated_path.stat().st_mode), 0o600)
        finally:
            server.server_close()

    def test_generated_token_is_reused_safely_across_server_restarts(self):
        path = Path(self.temp.name) / "reused.token"
        first = create_tcp_server(self.query, port=0, token_path=path)
        token = first.bearer_token
        first.server_close()
        second = create_tcp_server(self.query, port=0, token_path=path)
        try:
            self.assertEqual(second.bearer_token, token)
            self.assertEqual(path.read_text(encoding="utf-8"), token)
            thread = start(second)
            port = second.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/v1/health", headers={
                "Host": f"127.0.0.1:{port}",
                "Authorization": f"Bearer {token}",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            second.shutdown()
            thread.join(2)
        finally:
            second.server_close()

    def test_unsafe_or_mismatched_existing_token_files_are_rejected(self):
        unsafe = Path(self.temp.name) / "unsafe.token"
        unsafe.write_text(TOKEN, encoding="ascii")
        unsafe.chmod(0o644)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, token_path=unsafe)

        mismatch = Path(self.temp.name) / "mismatch.token"
        mismatch.write_text("x" * 48, encoding="ascii")
        mismatch.chmod(0o600)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, bearer_token=TOKEN, token_path=mismatch)

        target = Path(self.temp.name) / "target.token"
        target.write_text(TOKEN, encoding="ascii")
        target.chmod(0o600)
        link = Path(self.temp.name) / "link.token"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, token_path=link)


class TCPRateLimitTests(unittest.TestCase):
    def test_burst_refill_and_concurrency_are_bounded_per_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            now = [100.0]
            server = create_tcp_server(
                query, port=0, bearer_token=TOKEN,
                token_path=Path(temporary) / "token",
                rate_limit_capacity=2,
                rate_limit_refill_per_second=1.0,
                monotonic=lambda: now[0],
            )
            thread = start(server)
            port = server.server_address[1]

            def request():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("GET", "/v1/health", headers={
                    "Host": f"127.0.0.1:{port}",
                    "Authorization": f"Bearer {TOKEN}",
                })
                response = connection.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                connection.close()
                return result

            self.assertEqual([request()[0], request()[0]], [200, 200])
            status, headers, body = request()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")
            self.assertEqual(headers["Retry-After"], "1")
            now[0] += 1.0
            self.assertEqual(request()[0], 200)

            now[0] += 10.0
            results = []
            workers = [threading.Thread(target=lambda: results.append(request()[0])) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
            self.assertEqual(results.count(200), 2)
            self.assertEqual(results.count(429), 6)
            server.shutdown()
            server.server_close()
            thread.join(2)
            store.close()


if __name__ == "__main__":
    unittest.main()
