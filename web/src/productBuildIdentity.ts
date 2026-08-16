export type ProductBuildIdentity = Readonly<{
  candidateVersion: string;
  candidateBuild: string;
  channel: string;
  releaseStage: string;
  publicationStatus: string;
  releaseEligible: boolean;
  publishedBaselineTag: string;
  canaryQualifiedMachines: number;
  canaryTargetMachines: number;
  canaryClock: string;
}>;

export type ProductBuildIdentityCopy = Readonly<{
  versionWithBuild: string;
  stageCandidate: string;
  stagePrereleaseReady: string;
  stagePublishedPrerelease: string;
  publicationNotPublished: string;
  stateUnknown: string;
  publishedStable: string;
  canaryFormat: string;
  canaryNotStarted: string;
  canaryNotStartedStatus?: string;
  canaryRunning: string;
  canaryPassed: string;
  canaryBlocked: string;
  canaryUnknown: string;
}>;

export type ProductBuildIdentityPresentation = Readonly<{
  versionAndBuild: string;
  lifecycle: string;
  published: string;
  canary: string;
}>;

function format(
  template: string,
  values: Readonly<Record<string, string | number>>,
): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) =>
    String(values[key] ?? ""),
  );
}

function lifecycle(
  identity: ProductBuildIdentity,
  copy: ProductBuildIdentityCopy,
): string {
  if (
    identity.releaseStage === "candidate" &&
    identity.publicationStatus === "not_published" &&
    identity.releaseEligible === false
  ) {
    return `${copy.stageCandidate} · ${copy.publicationNotPublished}`;
  }
  if (
    identity.releaseStage === "prerelease_ready" &&
    identity.publicationStatus === "not_published" &&
    identity.releaseEligible === true
  ) {
    return `${copy.stagePrereleaseReady} · ${copy.publicationNotPublished}`;
  }
  if (
    identity.releaseStage === "prerelease_published" &&
    identity.publicationStatus === "published_prerelease" &&
    identity.releaseEligible === false
  ) {
    return copy.stagePublishedPrerelease;
  }
  return copy.stateUnknown;
}

function canaryStatus(
  clock: string,
  copy: ProductBuildIdentityCopy,
): string {
  switch (clock) {
    case "not_started":
      return copy.canaryNotStartedStatus ?? copy.canaryNotStarted;
    case "running":
      return copy.canaryRunning;
    case "passed":
      return copy.canaryPassed;
    case "blocked":
      return copy.canaryBlocked;
    default:
      return copy.canaryUnknown;
  }
}

export function buildProductIdentityPresentation(
  identity: ProductBuildIdentity,
  copy: ProductBuildIdentityCopy,
): ProductBuildIdentityPresentation {
  const version = `${identity.candidateVersion} ${identity.channel.toUpperCase()}`;
  return Object.freeze({
    versionAndBuild: format(copy.versionWithBuild, {
      version,
      build: identity.candidateBuild,
    }),
    lifecycle: lifecycle(identity, copy),
    published: format(copy.publishedStable, {
      version: identity.publishedBaselineTag,
    }),
    canary: format(copy.canaryFormat, {
      status: canaryStatus(identity.canaryClock, copy),
      qualified: identity.canaryQualifiedMachines,
      target: identity.canaryTargetMachines,
    }),
  });
}
