"use strict";

function format(template, values) {
  return template.replace(/\{(\w+)\}/g, (_match, key) =>
    String(values[key] ?? ""),
  );
}

function lifecycle(identity, copy) {
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

function canaryStatus(clock, copy) {
  switch (clock) {
    case "not_started":
      return copy.canaryNotStarted;
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

function buildProductIdentityPresentation(identity, copy) {
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

module.exports = Object.freeze({ buildProductIdentityPresentation });
