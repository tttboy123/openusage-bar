import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const productVersionModule = require("../product_version_truth.js");
const { formatCandidateVersion, productVersionTruth } = productVersionModule;

const expectedIdentity = {
  displayName: "UsageHub",
  legacyDisplayName: "OpenUsage Bar",
  candidateVersion: "0.8.6",
  candidateBuild: "28",
  channel: "rc",
  releaseStage: "candidate",
  publicationStatus: "not_published",
  releaseEligible: false,
  publishedBaselineVersion: "0.7.1",
  publishedBaselineTag: "v0.7.1",
  canaryQualifiedMachines: 0,
  canaryTargetMachines: 5,
  canaryClock: "not_started",
};

test("exports the closed renderer-safe product version projection", () => {
  assert.deepEqual(productVersionTruth, expectedIdentity);
  assert.deepEqual(Object.keys(productVersionTruth), Object.keys(expectedIdentity));
});

test("freezes the projection and module exports", () => {
  assert.equal(Object.isFrozen(productVersionTruth), true);
  assert.equal(Object.isFrozen(productVersionModule), true);
  assert.throws(() => {
    productVersionTruth.displayName = "Changed";
  }, TypeError);
});

test("formats version and channel without embedding final UI copy", () => {
  assert.equal(formatCandidateVersion(productVersionTruth), "0.8.6 RC");
  assert.equal(
    formatCandidateVersion({ candidateVersion: "1.2.3", channel: "beta" }),
    "1.2.3 BETA",
  );
});
