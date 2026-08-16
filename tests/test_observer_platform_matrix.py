from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import openusage_bar.keychain as keychain_module
import openusage_bar.provider_catalog as provider_catalog_module
from openusage_bar.provider_catalog import ObserverPlatformResolver, catalog


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "observer_platform_matrix.v1.json"
)
PLATFORM_MAPPINGS = {
    "darwin": {
        "catalog_operating_system": "macos",
        "runtime_platform": "darwin",
    },
    "linux": {
        "catalog_operating_system": "linux",
        "runtime_platform": "linux",
    },
    "windows": {
        "catalog_operating_system": "windows",
        "runtime_platform": "win32",
    },
}
EVIDENCE_FIELDS = {
    "credential_backend",
    "executable",
    "fact_parser",
    "local_discovery",
}
EVIDENCE_STATUSES = {"not_applicable", "unverified", "verified"}
REASON_CODES = {
    "existing_macos_contract",
    "hosted_native_evidence",
    "source_level_evidence_unverified",
}


def _load_fixture() -> dict[str, object]:
    with FIXTURE_PATH.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise AssertionError("Observer platform matrix must be an object")
    return payload


class ObserverPlatformMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _load_fixture()

    def test_fixture_schema_and_platform_evidence_are_strict(self) -> None:
        self.assertEqual(
            set(self.fixture),
            {"schema_version", "platforms", "sources"},
        )
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["platforms"], PLATFORM_MAPPINGS)

        sources = self.fixture["sources"]
        self.assertIsInstance(sources, list)
        self.assertEqual(
            [(entry["family_id"], entry["source_id"]) for entry in sources],
            sorted(
                (entry["family_id"], entry["source_id"])
                for entry in sources
            ),
        )
        for entry in sources:
            with self.subTest(
                family=entry.get("family_id"), source=entry.get("source_id")
            ):
                self.assertEqual(
                    set(entry), {"family_id", "source_id", "platforms"}
                )
                self.assertIsInstance(entry["family_id"], str)
                self.assertTrue(entry["family_id"])
                self.assertIsInstance(entry["source_id"], str)
                self.assertTrue(entry["source_id"])
                self.assertEqual(set(entry["platforms"]), set(PLATFORM_MAPPINGS))

                for platform, support in entry["platforms"].items():
                    with self.subTest(platform=platform):
                        self.assertEqual(
                            set(support),
                            {"evidence", "reason_code", "supported"},
                        )
                        self.assertIs(type(support["supported"]), bool)
                        self.assertIn(support["reason_code"], REASON_CODES)
                        self.assertEqual(
                            set(support["evidence"]), EVIDENCE_FIELDS
                        )
                        self.assertTrue(
                            set(support["evidence"].values())
                            <= EVIDENCE_STATUSES
                        )
                        fully_evidenced = all(
                            status != "unverified"
                            for status in support["evidence"].values()
                        )
                        self.assertEqual(support["supported"], fully_evidenced)
                        expected_reason = "source_level_evidence_unverified"
                        if support["supported"]:
                            expected_reason = (
                                "existing_macos_contract"
                                if platform == "darwin"
                                else "hosted_native_evidence"
                            )
                        self.assertEqual(
                            support["reason_code"], expected_reason
                        )

    def test_fixture_covers_every_catalog_family_and_source_exactly_once(
        self,
    ) -> None:
        fixture_pairs = [
            (entry["family_id"], entry["source_id"])
            for entry in self.fixture["sources"]
        ]
        catalog_pairs = {
            (family.family_id, source.source_id)
            for family in catalog.families
            for source in family.sources
        }

        self.assertEqual(len(fixture_pairs), 50)
        self.assertEqual(len(fixture_pairs), len(set(fixture_pairs)))
        self.assertEqual(set(fixture_pairs), catalog_pairs)
        self.assertEqual(
            {family_id for family_id, _ in fixture_pairs},
            set(catalog.family_ids),
        )
        self.assertEqual(len({family_id for family_id, _ in fixture_pairs}), 39)

    def test_catalog_never_declares_an_unverified_platform(self) -> None:
        entries = {
            (entry["family_id"], entry["source_id"]): entry
            for entry in self.fixture["sources"]
        }
        matrix_platform_by_catalog_os = {
            mapping["catalog_operating_system"]: platform
            for platform, mapping in PLATFORM_MAPPINGS.items()
        }

        for family in catalog.families:
            for source in family.sources:
                with self.subTest(
                    family=family.family_id, source=source.source_id
                ):
                    entry = entries[(family.family_id, source.source_id)]
                    supported_operating_systems = {
                        PLATFORM_MAPPINGS[platform]["catalog_operating_system"]
                        for platform, support in entry["platforms"].items()
                        if support["supported"]
                    }
                    self.assertTrue(
                        source.operating_systems
                        <= supported_operating_systems
                    )
                    for operating_system in source.operating_systems:
                        platform = matrix_platform_by_catalog_os[operating_system]
                        support = entry["platforms"][platform]
                        self.assertTrue(support["supported"])
                        self.assertNotIn(
                            "unverified", support["evidence"].values()
                        )

    def test_current_matrix_promotes_only_retained_hosted_source_evidence(
        self,
    ) -> None:
        promoted = {
            "windows": {("codex", "codex_local_log")},
            "linux": {
                ("codex", "codex_local_log"),
                ("moonshot", "moonshot_official_api"),
            },
        }
        for entry in self.fixture["sources"]:
            pair = (entry["family_id"], entry["source_id"])
            with self.subTest(
                family=entry["family_id"], source=entry["source_id"]
            ):
                self.assertTrue(entry["platforms"]["darwin"]["supported"])
                for platform, promoted_pairs in promoted.items():
                    support = entry["platforms"][platform]
                    self.assertEqual(support["supported"], pair in promoted_pairs)
                    self.assertEqual(
                        support["reason_code"],
                        "hosted_native_evidence"
                        if pair in promoted_pairs
                        else "source_level_evidence_unverified",
                    )

    def test_fixture_contains_capability_evidence_not_private_runtime_data(
        self,
    ) -> None:
        serialized = json.dumps(self.fixture, sort_keys=True).casefold()
        for forbidden in (
            "authorization",
            "bearer ",
            "cookie",
            "http://",
            "https://",
            "private key",
            "secret=",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)


class ObserverPlatformCapabilityContractTests(unittest.TestCase):
    def test_public_query_returns_stable_ordered_immutable_records(self) -> None:
        catalog_order = tuple(
            (family.family_id, source.source_id)
            for family in catalog.families
            for source in family.sources
        )
        source_by_pair = {
            (family.family_id, source.source_id): source
            for family in catalog.families
            for source in family.sources
        }

        for operating_system in ("macos", "windows", "linux"):
            with self.subTest(operating_system=operating_system):
                first = catalog.source_platform_capabilities(operating_system)
                second = catalog.source_platform_capabilities(operating_system)

                self.assertIsInstance(first, tuple)
                self.assertEqual(first, second)
                self.assertEqual(len(first), 50)
                self.assertEqual(
                    tuple(
                        (record.family_id, record.source_id)
                        for record in first
                    ),
                    catalog_order,
                )

                expected_supported_count = sum(
                    operating_system in source.operating_systems
                    for source in source_by_pair.values()
                )
                self.assertEqual(
                    sum(record.supported for record in first),
                    expected_supported_count,
                )
                for record in first:
                    pair = (record.family_id, record.source_id)
                    self.assertIn(pair, source_by_pair)
                    self.assertIs(type(record.supported), bool)
                    self.assertIn(
                        record.reason_code,
                        {
                            "supported_sources_available",
                            "source_level_evidence_unverified",
                        },
                    )
                    expected_supported = (
                        operating_system
                        in source_by_pair[pair].operating_systems
                    )
                    self.assertEqual(record.supported, expected_supported)
                    self.assertEqual(
                        record.reason_code,
                        "supported_sources_available"
                        if expected_supported
                        else "source_level_evidence_unverified",
                    )

                runtime_platform = {
                    "macos": "darwin",
                    "windows": "win32",
                    "linux": "linux",
                }[operating_system]
                self.assertEqual(
                    first,
                    ObserverPlatformResolver(
                        catalog, runtime_platform=runtime_platform
                    ).source_capabilities,
                )

                for field, replacement in (
                    ("family_id", "changed"),
                    ("source_id", "changed"),
                    ("supported", not first[0].supported),
                    ("reason_code", "changed"),
                ):
                    with self.subTest(field=field):
                        with self.assertRaises(
                            (AttributeError, FrozenInstanceError, TypeError)
                        ):
                            setattr(first[0], field, replacement)

    def test_public_query_is_pure_memory_and_uses_only_its_argument(self) -> None:
        forbidden = AssertionError("platform capability query touched the host")
        with (
            patch.object(
                keychain_module,
                "default_keychain",
                side_effect=forbidden,
            ),
            patch.object(
                keychain_module,
                "default_keychain_api",
                side_effect=forbidden,
            ),
            patch("builtins.open", side_effect=forbidden),
            patch.object(Path, "open", side_effect=forbidden),
            patch.object(os, "getenv", side_effect=forbidden),
            patch.object(socket, "create_connection", side_effect=forbidden),
            patch.object(socket, "socket", side_effect=forbidden),
            patch.object(subprocess, "Popen", side_effect=forbidden),
            patch.object(subprocess, "run", side_effect=forbidden),
        ):
            for operating_system in ("macos", "windows", "linux"):
                with self.subTest(operating_system=operating_system):
                    records = catalog.source_platform_capabilities(
                        operating_system
                    )
                    self.assertEqual(len(records), 50)

    def test_public_query_rejects_unknown_or_runtime_platform_names(self) -> None:
        private_value = "/private/example/runtime"
        for value in (
            "darwin",
            "win32",
            "unix",
            "MACOS",
            "",
            private_value,
            None,
            True,
            1,
            object(),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as raised:
                    catalog.source_platform_capabilities(value)
                self.assertNotIn(private_value, str(raised.exception))

    def test_module_has_no_import_time_distribution_validation(self) -> None:
        source_path = Path(provider_catalog_module.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        provider_catalog_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ProviderCatalog"
        )
        self.assertIn(
            "require_operating_system",
            {
                node.name
                for node in provider_catalog_class.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            },
        )

        import_time_distribution_calls = [
            node
            for statement in tree.body
            if not isinstance(
                statement,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "require_operating_system"
        ]
        self.assertEqual(import_time_distribution_calls, [])


if __name__ == "__main__":
    unittest.main()
