"use strict";

// Build-time public projection of product-version-truth/v1. The consistency
// verifier keeps these non-secret facts aligned with the machine contract.
const productVersionTruth = Object.freeze({
  displayName: "UsageHub",
  legacyDisplayName: "OpenUsage Bar",
  candidateVersion: "0.8.7",
  candidateBuild: "29",
  channel: "rc",
  releaseStage: "candidate",
  publicationStatus: "not_published",
  releaseEligible: false,
  publishedBaselineVersion: "0.7.1",
  publishedBaselineTag: "v0.7.1",
  canaryQualifiedMachines: 0,
  canaryTargetMachines: 5,
  canaryClock: "not_started",
});

function formatCandidateVersion({ candidateVersion, channel }) {
  return `${candidateVersion} ${channel.toUpperCase()}`;
}

module.exports = Object.freeze({ formatCandidateVersion, productVersionTruth });
