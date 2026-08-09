from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "openusage_bar/resources/product-version-truth.v1.json"
IDENTITY = ROOT / "openusage_bar/resources/artifact-build-identity.v1.json"
SCHEMA = ROOT / "docs/schemas/artifact-build-identity-v1.schema.json"
PACKAGED_NAME = "product-build-identity.v1.json"
WEB_COPY = ROOT / "web/public" / PACKAGED_NAME
SWIFT_COPY = ROOT / "swift_app/Resources" / PACKAGED_NAME

IDENTITY_FIELDS = {
    "candidateBuild",
    "candidateVersion",
    "canaryClock",
    "canaryQualifiedMachines",
    "canaryTargetMachines",
    "channel",
    "displayName",
    "publicationStatus",
    "publishedBaselineTag",
    "publishedBaselineVersion",
    "releaseEligible",
    "releaseStage",
    "schemaVersion",
}


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("ascii")


class ArtifactBuildIdentityContractTests(unittest.TestCase):
    def test_identity_is_closed_canonical_and_derived_from_product_truth(self):
        truth = json.loads(TRUTH.read_text(encoding="utf-8"))
        identity = json.loads(IDENTITY.read_text(encoding="ascii"))
        candidate = truth["candidate"]
        baseline = truth["publishedBaseline"]
        canary = candidate["canary"]

        self.assertEqual(set(identity), IDENTITY_FIELDS)
        self.assertEqual(identity["schemaVersion"], "artifact-build-identity/v1")
        self.assertEqual(
            identity,
            {
                "candidateBuild": candidate["build"],
                "candidateVersion": candidate["version"],
                "canaryClock": canary["clock"],
                "canaryQualifiedMachines": canary["qualifiedMachines"],
                "canaryTargetMachines": canary["targetMachines"],
                "channel": candidate["channel"],
                "displayName": truth["product"]["displayName"],
                "publicationStatus": candidate["publicationStatus"],
                "publishedBaselineTag": baseline["tag"],
                "publishedBaselineVersion": baseline["version"],
                "releaseEligible": candidate["releaseEligible"],
                "releaseStage": candidate["releaseStage"],
                "schemaVersion": "artifact-build-identity/v1",
            },
        )
        self.assertEqual(IDENTITY.read_bytes(), _canonical_json(identity))
        self.assertNotIn("publicationReceipt", identity)
        self.assertNotIn("legacyDisplayName", identity)

    def test_schema_closes_every_artifact_identity_field(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), IDENTITY_FIELDS)
        self.assertEqual(set(schema["properties"]), IDENTITY_FIELDS)
        self.assertEqual(
            schema["properties"]["schemaVersion"]["const"],
            "artifact-build-identity/v1",
        )
        self.assertNotIn("publicationReceipt", schema["properties"])

    def test_web_and_swift_package_the_exact_canonical_bytes_at_fixed_paths(self):
        canonical = IDENTITY.read_bytes()

        self.assertEqual(WEB_COPY.read_bytes(), canonical)
        self.assertEqual(SWIFT_COPY.read_bytes(), canonical)
        self.assertEqual(WEB_COPY.name, PACKAGED_NAME)
        self.assertEqual(SWIFT_COPY.name, PACKAGED_NAME)

        build_script = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
        self.assertIn("artifact-build-identity.v1.json", build_script)
        self.assertIn("Contents/Resources/product-build-identity.v1.json", build_script)

    def test_electron_maps_identity_to_resources_and_native_build_28(self):
        package = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
        build = package["build"]

        self.assertEqual(build["buildVersion"], "28")
        mappings = build["extraResources"]
        self.assertIn(
            {
                "from": "../openusage_bar/resources/artifact-build-identity.v1.json",
                "to": PACKAGED_NAME,
            },
            mappings,
        )
        self.assertNotIn("publicationReceipt", json.dumps(mappings, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
