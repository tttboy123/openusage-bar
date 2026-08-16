from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

from openusage_bar.gateway import decision_trace


ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "gateway-decision-trace.openusage/v1"
TRACE_FIELDS = {
    "traceId",
    "occurredAt",
    "kind",
    "execution",
    "outcome",
    "reason",
    "pool",
    "selected",
    "exclusions",
    "fallback",
    "factsWindow",
}
KINDS = {"route_advice", "gateway_execution", "pool_selection"}
EXECUTIONS = {"advice_only", "executed"}
OUTCOMES = {
    "yes",
    "no",
    "defer",
    "selected",
    "unavailable",
    "succeeded",
    "failed",
}
REASONS = {
    "approaching_limit",
    "burn_rate_too_high",
    "quota_healthy",
    "quota_low",
    "quota_unknown",
}
STRATEGIES = {
    "fixed-first",
    "round-robin",
    "sticky",
    "quota-aware",
    "cost",
    "latency",
    "reliability",
}
PROVIDERS = {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
EXCLUSION_REASONS = {
    "cooldown",
    "credential_backend_unavailable",
    "cross_model_unconfirmed",
    "cross_provider_unconfirmed",
    "cross_region_unconfirmed",
    "disabled",
    "health_unknown",
    "metric_unknown",
    "quota_unknown",
    "unhealthy",
}
FALLBACK_ACTIONS = {"none", "retry", "fail", "degrade_to_cheap"}
LOCAL_API_N_MINUS_ONE_SHA256 = {
    "tests/fixtures/local-api-v1/v0.4.2.schema.json": (
        "94b4e8d3d32270814482a6effd0366dd29535161ec759e2d81c15d85a9a30a6e"
    ),
    "tests/fixtures/local-api-v1/v0.4.2.snapshot.json": (
        "86b36bbc0f25fc1bae57c73ba898d57d15a1e88f46fdc798c5bfcd3180f9f498"
    ),
}


def _quoted_array(source: str, marker: str) -> set[str]:
    start = source.index(marker)
    opening = source.index("[", start)
    closing = source.index("]", opening)
    return set(re.findall(r'"([^"\\]+)"', source[opening : closing + 1]))


def _route_trace() -> dict[str, object]:
    return {
        "traceId": "trace_00000000000000000000000000000003",
        "occurredAt": "2026-08-10T12:00:03.000000Z",
        "kind": "route_advice",
        "execution": "advice_only",
        "outcome": "defer",
        "reason": "quota_low",
        "pool": None,
        "selected": None,
        "exclusions": [],
        "fallback": None,
        "factsWindow": {"durationSeconds": 300},
    }


def _gateway_trace() -> dict[str, object]:
    return {
        "traceId": "trace_00000000000000000000000000000002",
        "occurredAt": "2026-08-10T12:00:02.000000Z",
        "kind": "gateway_execution",
        "execution": "executed",
        "outcome": "succeeded",
        "reason": None,
        "pool": None,
        "selected": {"providerId": "openai", "accountDisplayId": None},
        "exclusions": [],
        "fallback": {
            "attempted": False,
            "attemptCount": 1,
            "finalAction": "none",
        },
        "factsWindow": None,
    }


def _pool_trace() -> dict[str, object]:
    return {
        "traceId": "trace_00000000000000000000000000000001",
        "occurredAt": "2026-08-10T12:00:01.000000Z",
        "kind": "pool_selection",
        "execution": "executed",
        "outcome": "selected",
        "reason": None,
        "pool": {"poolId": "primary-us", "revision": 7, "strategy": "quota-aware"},
        "selected": {
            "providerId": "anthropic",
            "accountDisplayId": "acct_1234567890ab",
        },
        "exclusions": [
            {"accountDisplayId": "acct_abcdef123456", "reason": "cooldown"}
        ],
        "fallback": None,
        "factsWindow": None,
    }


def _desktop_sanitize(values: list[object]) -> list[object]:
    script = r"""
const fs = require("fs");
const { sanitizeDecisionTracesValue } = require("./desktop/gateway_proxy.js");
const values = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(values.map(sanitizeDecisionTracesValue)));
"""
    try:
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            input=json.dumps(values),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError("desktop Decision Trace sanitizer timed out") from error
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class DecisionTraceCrossLayerContractTests(unittest.TestCase):
    def test_schema_is_the_exact_eleven_field_allowlisted_contract(self) -> None:
        schema = json.loads(
            (
                ROOT
                / "openusage_bar/resources/gateway-decision-trace-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        properties = schema["$defs"]["traceBase"]["properties"]
        self.assertEqual(set(properties), TRACE_FIELDS)
        self.assertEqual(set(schema["$defs"]["traceBase"]["required"]), TRACE_FIELDS)
        self.assertFalse(schema["$defs"]["traceBase"]["additionalProperties"])
        self.assertEqual(schema["properties"]["apiVersion"]["const"], API_VERSION)
        self.assertEqual(schema["properties"]["traces"]["maxItems"], 128)
        self.assertEqual(properties["exclusions"]["maxItems"], 64)
        self.assertEqual(
            schema["$defs"]["factsWindow"]["properties"]["durationSeconds"][
                "maximum"
            ],
            2_678_400,
        )
        self.assertEqual(set(properties["kind"]["enum"]), KINDS)
        self.assertEqual(set(properties["execution"]["enum"]), EXECUTIONS)
        self.assertEqual(set(properties["outcome"]["enum"]), OUTCOMES)
        self.assertEqual(set(properties["reason"]["enum"]), REASONS | {None})
        self.assertEqual(
            set(schema["$defs"]["pool"]["properties"]["strategy"]["enum"]),
            STRATEGIES,
        )
        self.assertEqual(
            set(schema["$defs"]["selected"]["properties"]["providerId"]["enum"]),
            PROVIDERS | {None},
        )
        self.assertEqual(
            set(schema["$defs"]["exclusion"]["properties"]["reason"]["enum"]),
            EXCLUSION_REASONS,
        )
        self.assertEqual(
            set(schema["$defs"]["fallback"]["properties"]["finalAction"]["enum"]),
            FALLBACK_ACTIONS,
        )

    def test_python_desktop_and_web_constants_match_the_schema(self) -> None:
        desktop = (ROOT / "desktop/gateway_proxy.js").read_text(encoding="utf-8")
        web = (ROOT / "web/src/decisionTraces.ts").read_text(encoding="utf-8")

        self.assertEqual(decision_trace.DECISION_TRACE_API_VERSION, API_VERSION)
        self.assertEqual(decision_trace.MAX_DECISION_TRACES, 128)
        self.assertEqual(decision_trace.MAX_TRACE_EXCLUSIONS, 64)
        self.assertEqual(decision_trace.MAX_FACTS_WINDOW_SECONDS, 2_678_400)
        for source in (desktop, web):
            self.assertIn(f'"{API_VERSION}"', source)
            self.assertIn("128", source)
            self.assertIn("64", source)
            self.assertRegex(source, r"2(?:_)?678(?:_)?400")

        desktop_sets = {
            "const DECISION_TRACE_KINDS": KINDS,
            "const DECISION_TRACE_OUTCOMES": OUTCOMES,
            "const ACCOUNT_POOL_STRATEGIES": STRATEGIES,
            "const DECISION_TRACE_PROVIDERS": PROVIDERS,
            "const DECISION_TRACE_EXCLUSION_REASONS": EXCLUSION_REASONS,
            "const DECISION_TRACE_FALLBACK_ACTIONS": FALLBACK_ACTIONS,
        }
        web_sets = {
            "const TRACE_KINDS": KINDS,
            "const EXECUTIONS": EXECUTIONS,
            "const OUTCOMES": OUTCOMES,
            "const REASONS": REASONS,
            "const STRATEGIES": STRATEGIES,
            "const PROVIDERS": PROVIDERS,
            "const EXCLUSION_REASONS": EXCLUSION_REASONS,
            "const FALLBACK_ACTIONS": FALLBACK_ACTIONS,
        }
        for marker, expected in desktop_sets.items():
            self.assertEqual(_quoted_array(desktop, marker), expected, marker)
        for marker, expected in web_sets.items():
            self.assertEqual(_quoted_array(web, marker), expected, marker)

        self.assertEqual(
            _quoted_array(web, "const TRACE_FIELDS"), TRACE_FIELDS
        )
        self.assertEqual(
            _quoted_array(desktop, "const trace = ownDataRecord(value"), TRACE_FIELDS
        )
        self.assertIn("export function normalizeDecisionTraces", web)

    def test_python_and_desktop_have_the_same_public_accept_reject_boundary(self) -> None:
        valid = {
            "apiVersion": API_VERSION,
            "traces": [_route_trace(), _gateway_trace(), _pool_trace()],
        }
        invalid: list[dict[str, object]] = []
        forbidden = (
            "prompt",
            "response",
            "rawError",
            "credential",
            "token",
            "header",
            "endpoint",
            "path",
            "accountId",
        )
        for field in forbidden:
            candidate = copy.deepcopy(valid)
            candidate["traces"][0][field] = f"PRIVATE_{field.upper()}_CANARY"
            invalid.append(candidate)
        cross_field_invalid = copy.deepcopy(valid)
        cross_field_invalid["traces"][0]["execution"] = "executed"
        invalid.append(cross_field_invalid)
        private_provider = copy.deepcopy(valid)
        private_provider["traces"][1]["selected"]["providerId"] = "private-provider"
        invalid.append(private_provider)
        private_account = copy.deepcopy(valid)
        private_account["traces"][2]["selected"] = {
            "providerId": "anthropic",
            "accountId": "private-account-id",
        }
        invalid.append(private_account)
        wrong_order = copy.deepcopy(valid)
        wrong_order["traces"].reverse()
        invalid.append(wrong_order)

        candidates: list[object] = [valid, *invalid]
        python_results = [
            decision_trace.validate_decision_traces_payload(candidate)
            for candidate in candidates
        ]
        desktop_results = _desktop_sanitize(candidates)
        self.assertEqual(python_results[0], valid)
        self.assertEqual(desktop_results[0], valid)
        self.assertEqual(python_results[1:], [None] * len(invalid))
        self.assertEqual(desktop_results[1:], [None] * len(invalid))

    def test_local_api_v1_n_minus_one_release_evidence_is_byte_identical(self) -> None:
        for relative, expected in LOCAL_API_N_MINUS_ONE_SHA256.items():
            with self.subTest(relative=relative):
                actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)

    def test_decision_trace_does_not_expand_the_local_api_v1_surface(self) -> None:
        local_schema = (
            ROOT / "openusage_bar/resources/local-api-v1.schema.json"
        ).read_text(encoding="utf-8")
        local_router = (ROOT / "openusage_bar/local_api.py").read_text(
            encoding="utf-8"
        )
        for private_gateway_term in (
            "decisionTrace",
            "decision-traces",
            "gateway-decision-trace",
            "/gateway/",
        ):
            with self.subTest(private_gateway_term=private_gateway_term):
                self.assertNotIn(private_gateway_term, local_schema)
                self.assertNotIn(private_gateway_term, local_router)

    def test_pool_selection_is_an_optional_seam_not_runtime_wiring(self) -> None:
        pools = (ROOT / "openusage_bar/gateway/pools.py").read_text(encoding="utf-8")
        collector = (ROOT / "openusage_bar/collector_cli.py").read_text(encoding="utf-8")
        api = (ROOT / "openusage_bar/gateway/api.py").read_text(encoding="utf-8")
        self.assertIn("decision_traces: DecisionTraceRecorder | None = None", pools)
        self.assertIn("record_pool_selection", pools)
        self.assertNotIn("PoolSelector(", collector)
        self.assertNotIn("PoolSelector(", api)

    def test_process_lifetime_boundary_is_declared_in_server_and_ui_copy(self) -> None:
        server = (ROOT / "openusage_bar/gateway/decision_trace.py").read_text(
            encoding="utf-8"
        )
        api_guide = (ROOT / "docs/api/gateway-api-v1.md").read_text(
            encoding="utf-8"
        )
        i18n = (ROOT / "web/src/i18n.ts").read_text(encoding="utf-8")
        self.assertIn("process-lifetime", server)
        self.assertRegex(server, r"discarded when (?:that|the) process exits")
        self.assertIn("GET /gateway/v1/decision-traces", api_guide)
        self.assertIn("gateway-decision-trace.openusage/v1", api_guide)
        self.assertIn("not a durable audit history", api_guide)
        self.assertIn("does not imply", api_guide)
        self.assertRegex(i18n, r"Gateway process")
        self.assertRegex(i18n, r"restart")
        self.assertRegex(i18n, r"Gateway 进程")
        self.assertRegex(i18n, r"重启")

    def test_desktop_build_runs_and_is_triggered_by_every_new_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/desktop-build.yml").read_text(
            encoding="utf-8"
        )
        for path in (
            "openusage_bar/**",
            "desktop/**",
            "web/**",
            "tests/test_gateway_*.py",
            "tests/test_decision_trace_cross_layer_contract.py",
        ):
            self.assertGreaterEqual(workflow.count(f'- "{path}"'), 2, path)
        for command in (
            "tests.test_gateway_decision_trace",
            "tests.test_decision_trace_cross_layer_contract",
            "npm test",
            "npm run test:e2e:decision-traces",
        ):
            self.assertIn(command, workflow)


if __name__ == "__main__":
    unittest.main()
