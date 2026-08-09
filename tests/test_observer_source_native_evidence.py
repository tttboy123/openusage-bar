from __future__ import annotations

import copy
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - optional release oracle
    Draft202012Validator = None

from scripts import observer_source_native_evidence as evidence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/schemas/observer-source-native-evidence-v1.schema.json"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def source_record(source: str, *, verified: bool = True) -> dict[str, object]:
    if source == "moonshot":
        family_id = "moonshot"
        catalog_source_id = "moonshot_official_api"
        runtime_source_ids = ["moonshot.balance"]
        seams = {
            "executableDiscovery": "not_applicable",
            "localFileDiscovery": "not_applicable",
            "credentialBackend": "verified" if verified else "not_verified",
            "factParser": "verified",
        }
    elif source == "codex":
        family_id = "codex"
        catalog_source_id = "codex_local_log"
        runtime_source_ids = [
            "codex.local_rate_limits",
            "codex.local_sessions",
        ]
        seams = {
            "executableDiscovery": "not_applicable",
            "localFileDiscovery": "verified" if verified else "not_verified",
            "credentialBackend": "not_applicable",
            "factParser": "verified",
        }
    else:  # pragma: no cover - test helper contract
        raise AssertionError(source)
    return {
        "familyId": family_id,
        "catalogSourceId": catalog_source_id,
        "runtimeSourceIds": runtime_source_ids,
        "status": "verified" if verified else "unverified",
        "reasonCode": (
            "all_required_seams_verified"
            if verified
            else "required_seam_unverified"
        ),
        "seams": seams,
    }


def valid_payload(
    platform: str = "windows",
    *,
    moonshot_verified: bool = True,
    codex_verified: bool = True,
) -> dict[str, object]:
    sources = [
        source_record("moonshot", verified=moonshot_verified),
        source_record("codex", verified=codex_verified),
    ]
    return {
        "schemaVersion": "observer-source-native-evidence/v1",
        "evidenceClass": "github_hosted_native_fixture",
        "platform": platform,
        "runnerEnvironment": "github-hosted",
        "evaluatedSourceCount": 2,
        "verifiedSourceCount": sum(
            item["status"] == "verified" for item in sources
        ),
        "sources": sources,
    }


class RoundTripKeychain:
    def __init__(self, *, existing: str | None = None) -> None:
        self.value = existing
        self.set_values: list[str] = []
        self.deleted = 0

    def get(self, _account: str) -> str | None:
        return self.value

    def set(self, _account: str, value: str) -> None:
        self.value = value
        self.set_values.append(value)

    def delete(self, _account: str) -> None:
        self.value = None
        self.deleted += 1


class PartialWriteKeychain(RoundTripKeychain):
    def set(self, _account: str, value: str) -> None:
        self.value = value
        self.set_values.append(value)
        raise RuntimeError("sanitized partial write")


class BrokenRoundTripKeychain(RoundTripKeychain):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get(self, _account: str) -> str | None:
        self.reads += 1
        if self.reads == 1:
            return None
        return "wrong-synthetic-value"


class MoonshotFixtureClient:
    def __init__(self, *, malformed: bool = False) -> None:
        self.malformed = malformed
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get_json(self, endpoint: str, headers: dict[str, str]):
        self.requests.append((endpoint, headers))
        if self.malformed:
            return {"code": 0, "data": {"available_balance": "-1"}}
        return {
            "code": 0,
            "data": {
                "available_balance": "12.34",
                "voucher_balance": "2",
                "cash_balance": "10.34",
            },
        }


def parser_failure_record(source: str) -> dict[str, object]:
    record = source_record(source, verified=False)
    seams = record["seams"]
    assert isinstance(seams, dict)
    seams["factParser"] = "not_verified"
    if source == "moonshot":
        seams["credentialBackend"] = "verified"
    else:
        seams["localFileDiscovery"] = "verified"
    return record


class ObserverSourceNativeEvidenceTests(unittest.TestCase):
    def test_public_surface_has_no_self_reported_platform_probe_or_home_input(self):
        self.assertEqual(
            list(inspect.signature(evidence.collect_native_evidence).parameters),
            [],
        )
        parser = evidence._parser()
        help_text = parser.format_help()
        for forbidden in (
            "--platform", "--home", "--secret", "--result", "--status"
        ):
            self.assertNotIn(forbidden, help_text)

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for evidence schema validation",
    )
    def test_committed_schema_is_current_meta_valid_and_accepts_partial_evidence(self):
        schema = evidence.render_schema()
        self.assertEqual(json.loads(SCHEMA.read_text(encoding="utf-8")), schema)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for payload in (
            valid_payload("windows"),
            valid_payload("linux", moonshot_verified=False),
            valid_payload("linux", codex_verified=False),
            valid_payload(
                "windows", moonshot_verified=False, codex_verified=False
            ),
        ):
            with self.subTest(
                platform=payload["platform"],
                verified=payload["verifiedSourceCount"],
            ):
                self.assertEqual(list(validator.iter_errors(payload)), [])
                self.assertEqual(evidence.validate_evidence(payload), payload)

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for hostile evidence validation",
    )
    def test_schema_and_validator_reject_crossed_states_private_fields_and_paths(self):
        schema_validator = Draft202012Validator(evidence.render_schema())
        base = valid_payload()
        mutations: dict[str, dict[str, object]] = {}
        mutations["private root"] = copy.deepcopy(base)
        mutations["private root"]["secretToken"] = "private"
        mutations["private source"] = copy.deepcopy(base)
        mutations["private source"]["sources"][0]["rawPayload"] = "private"
        mutations["path"] = copy.deepcopy(base)
        mutations["path"]["sources"][1]["runtimeSourceIds"][0] = "/private/path"
        mutations["count mismatch"] = copy.deepcopy(base)
        mutations["count mismatch"]["verifiedSourceCount"] = 1
        mutations["wrong platform"] = copy.deepcopy(base)
        mutations["wrong platform"]["platform"] = "macos"
        mutations["wrong order"] = copy.deepcopy(base)
        mutations["wrong order"]["sources"].reverse()
        mutations["crossed reason"] = copy.deepcopy(base)
        mutations["crossed reason"]["sources"][0][
            "reasonCode"
        ] = "required_seam_unverified"
        mutations["missing seam"] = copy.deepcopy(base)
        mutations["missing seam"]["sources"][1]["seams"][
            "localFileDiscovery"
        ] = "not_verified"

        for name, payload in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(list(schema_validator.iter_errors(payload)), [])
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_evidence(payload)

    def test_canonical_json_is_bounded_ascii_and_does_not_reflect_private_values(self):
        payload = valid_payload("linux", moonshot_verified=False)
        rendered = evidence.canonical_evidence_json(payload)
        self.assertEqual(rendered, evidence.canonical_evidence_json(payload))
        self.assertTrue(rendered.endswith("\n"))
        self.assertEqual(rendered, rendered.encode("ascii").decode("ascii"))
        self.assertLessEqual(len(rendered.encode("ascii")), 16 * 1024)
        for forbidden in (
            "/Users/", "/home/", "C:\\Users\\", "Bearer ",
            "api.moonshot", "rawPayload", "secretToken",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_moonshot_probe_round_trips_native_facade_parses_balance_and_cleans(self):
        keychain = RoundTripKeychain()
        client = MoonshotFixtureClient()

        record = evidence._probe_moonshot_native(
            keychain=keychain,
            client=client,
            clock=lambda: NOW,
        )

        self.assertEqual(record, source_record("moonshot"))
        self.assertEqual(keychain.deleted, 1)
        self.assertIsNone(keychain.value)
        self.assertEqual(len(keychain.set_values), 1)
        secret = keychain.set_values[0]
        self.assertTrue(secret)
        self.assertEqual(
            client.requests[0][1], {"Authorization": f"Bearer {secret}"}
        )
        self.assertNotIn(secret, evidence.canonical_evidence_json(
            valid_payload()
        ))

    def test_moonshot_probe_failure_is_unverified_sanitized_and_still_cleans(self):
        keychain = RoundTripKeychain()
        client = MoonshotFixtureClient(malformed=True)

        record = evidence._probe_moonshot_native(
            keychain=keychain,
            client=client,
            clock=lambda: NOW,
        )

        self.assertEqual(record, parser_failure_record("moonshot"))
        self.assertEqual(keychain.deleted, 1)
        self.assertIsNone(keychain.value)
        self.assertNotIn("-1", json.dumps(record, sort_keys=True))

    def test_moonshot_partial_write_is_cleaned_and_bad_round_trip_cannot_verify(self):
        partial = PartialWriteKeychain()
        partial_record = evidence._probe_moonshot_native(
            keychain=partial,
            client=MoonshotFixtureClient(),
            clock=lambda: NOW,
        )
        partial_expected = source_record("moonshot", verified=False)
        partial_expected["seams"]["factParser"] = "not_verified"
        self.assertEqual(partial_record, partial_expected)
        self.assertIsNone(partial.value)
        self.assertEqual(partial.deleted, 1)

        broken = BrokenRoundTripKeychain()
        client = MoonshotFixtureClient()
        with self.assertRaisesRegex(
            evidence.EvidenceError, "credential_state_changed"
        ):
            evidence._probe_moonshot_native(
                keychain=broken,
                client=client,
                clock=lambda: NOW,
            )
        self.assertEqual(client.requests, [])
        self.assertEqual(broken.deleted, 0)

    def test_moonshot_probe_refuses_existing_credential_without_overwriting(self):
        keychain = RoundTripKeychain(existing="existing-private-value")
        with self.assertRaisesRegex(
            evidence.EvidenceError, "credential_state_not_clean"
        ):
            evidence._probe_moonshot_native(
                keychain=keychain,
                client=MoonshotFixtureClient(),
                clock=lambda: NOW,
            )
        self.assertEqual(keychain.value, "existing-private-value")
        self.assertEqual(keychain.set_values, [])
        self.assertEqual(keychain.deleted, 0)

    def test_codex_probe_uses_default_home_shape_proves_both_parsers_and_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            record = evidence._probe_codex_native(
                home=home,
                clock=lambda: NOW,
            )

            self.assertEqual(record, source_record("codex"))
            self.assertFalse((home / ".codex/sessions").exists())
            self.assertFalse(
                (home / ".local/state/openusage-bar/codex-session-cache.json")
                .exists()
            )

    def test_codex_probe_refuses_existing_or_symlinked_default_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / ".codex/sessions"
            sessions.mkdir(parents=True)
            sentinel = sessions / "private-existing.jsonl"
            sentinel.write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(
                evidence.EvidenceError, "codex_state_not_clean"
            ):
                evidence._probe_codex_native(home=home, clock=lambda: NOW)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "private")

        if os.name != "nt":
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                target = home / "target"
                target.mkdir()
                sessions = home / ".codex/sessions"
                sessions.parent.mkdir(parents=True)
                sessions.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(
                    evidence.EvidenceError, "codex_state_not_clean"
                ):
                    evidence._probe_codex_native(home=home, clock=lambda: NOW)

    def test_codex_cleanup_preserves_replaced_foreign_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "owned.jsonl"
            owned.write_text("owned", encoding="utf-8")
            identity = evidence._file_identity(owned)
            replacement = root / "replacement.jsonl"
            replacement.write_text("foreign-private", encoding="utf-8")
            os.replace(replacement, owned)

            with self.assertRaisesRegex(
                evidence.EvidenceError, "codex_cleanup_failed"
            ):
                evidence._cleanup_codex_paths(
                    owned_files={owned: identity}, created=[]
                )

            self.assertEqual(
                owned.read_text(encoding="utf-8"), "foreign-private"
            )

    def test_collect_allows_independent_source_results_but_counts_only_verified(self):
        moonshot = source_record("moonshot", verified=False)
        codex = source_record("codex")
        with (
            mock.patch.object(evidence.sys, "platform", "linux"),
            mock.patch.dict(
                os.environ,
                {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"},
                clear=False,
            ),
            mock.patch.object(
                evidence, "_probe_moonshot_native", return_value=moonshot
            ),
            mock.patch.object(
                evidence, "_probe_codex_native", return_value=codex
            ),
        ):
            payload = evidence.collect_native_evidence()

        self.assertEqual(payload, valid_payload(
            "linux", moonshot_verified=False, codex_verified=True
        ))

    def test_probe_cli_fails_closed_off_host_without_echo_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            private_output = Path(directory) / "private-output.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(evidence.sys, "platform", "darwin"),
                mock.patch.dict(os.environ, {}, clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = evidence.main(["probe", "--output", str(private_output)])

            self.assertEqual(result, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "observer_source_native_evidence_invalid "
                "reason=probe_environment_invalid\n",
            )
            self.assertNotIn(str(private_output), stderr.getvalue())
            self.assertFalse(private_output.exists())

    def test_cli_sanitizes_argument_errors_and_verifies_only_canonical_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_value = str(root / "private-cli-canary-secret")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = evidence.main(["probe", "--home", private_value])
            self.assertEqual(result, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "observer_source_native_evidence_invalid "
                "reason=arguments_invalid\n",
            )
            self.assertNotIn(private_value, stderr.getvalue())

            evidence_path = root / "evidence.json"
            evidence_path.write_text(
                evidence.canonical_evidence_json(valid_payload("linux")),
                encoding="ascii",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = evidence.main(
                    ["verify", "--evidence", str(evidence_path)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

            duplicate = root / "duplicate-private-name.json"
            duplicate.write_text(
                evidence_path.read_text(encoding="ascii").replace(
                    '"platform":"linux"',
                    '"platform":"linux","platform":"windows"',
                ),
                encoding="ascii",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = evidence.main(
                    ["verify", "--evidence", str(duplicate)]
                )
            self.assertEqual(result, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "observer_source_native_evidence_invalid "
                "reason=evidence_invalid\n",
            )
            self.assertNotIn(str(duplicate), stderr.getvalue())

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for validator parity",
    )
    def test_machine_validator_matches_schema_for_boolean_count_and_parser_failure(self):
        validator = Draft202012Validator(evidence.render_schema())
        boolean_count = valid_payload(
            "linux", moonshot_verified=False, codex_verified=True
        )
        boolean_count["verifiedSourceCount"] = True
        self.assertNotEqual(list(validator.iter_errors(boolean_count)), [])
        with self.assertRaises(evidence.EvidenceError):
            evidence.validate_evidence(boolean_count)

        parser_failure = valid_payload("linux")
        parser_failure["sources"][0] = parser_failure_record("moonshot")
        parser_failure["verifiedSourceCount"] = 1
        self.assertEqual(list(validator.iter_errors(parser_failure)), [])
        self.assertEqual(
            evidence.validate_evidence(parser_failure), parser_failure
        )


if __name__ == "__main__":
    unittest.main()
