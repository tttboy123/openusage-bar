// Build-time public projection of product-version-truth/v1. The consistency
// verifier keeps these non-secret facts aligned with the machine contract.
export type ProductVersionTruth = Readonly<{
  displayName: string;
  legacyDisplayName: string;
  candidateVersion: string;
  candidateBuild: string;
  channel: string;
  releaseStage: string;
  publicationStatus: string;
  releaseEligible: boolean;
  publishedBaselineVersion: string;
  publishedBaselineTag: string;
  canaryQualifiedMachines: number;
  canaryTargetMachines: number;
  canaryClock: string;
}>;

export const productVersionTruth: ProductVersionTruth = Object.freeze({
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
});

export function formatCandidateVersion(
  identity: Pick<ProductVersionTruth, "candidateVersion" | "channel">,
): string {
  return `${identity.candidateVersion} ${identity.channel.toUpperCase()}`;
}
