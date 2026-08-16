import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import test from "node:test";


const mainSource = await readFile(new URL("../main.js", import.meta.url), "utf8");
const require = createRequire(import.meta.url);

const BASE_IDENTITY = Object.freeze({
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

const COPY = Object.freeze({
  en: Object.freeze({
    versionWithBuild: "{version} (build {build})",
    stageCandidate: "Candidate",
    stagePrereleaseReady: "Pre-release ready",
    stagePublishedPrerelease: "Published pre-release",
    publicationNotPublished: "Not published",
    stateUnknown: "Unknown",
    publishedStable: "Published stable: {version}",
    canaryFormat: "Canary: {status} · {qualified}/{target}",
    canaryNotStarted: "Not started",
    canaryRunning: "Running",
    canaryPassed: "Passed",
    canaryBlocked: "Blocked",
    canaryUnknown: "Unknown",
  }),
  zh: Object.freeze({
    versionWithBuild: "{version}（构建 {build}）",
    stageCandidate: "候选版",
    stagePrereleaseReady: "预发布就绪",
    stagePublishedPrerelease: "已发布预览版",
    publicationNotPublished: "尚未发布",
    stateUnknown: "未知",
    publishedStable: "已发布稳定版：{version}",
    canaryFormat: "Canary：{status} · {qualified}/{target}",
    canaryNotStarted: "尚未开始",
    canaryRunning: "进行中",
    canaryPassed: "已通过",
    canaryBlocked: "已阻塞",
    canaryUnknown: "未知",
  }),
});

function presentationModule() {
  return require("../build_identity_copy.js");
}

function identity(overrides = {}) {
  return { ...BASE_IDENTITY, ...overrides };
}


test("custom About distinguishes the candidate from the published baseline", () => {
  assert.match(
    mainSource,
    /const\s*\{[^}]*buildProductIdentityPresentation[^}]*\}\s*=\s*require\("\.\/build_identity_copy"\)/,
  );
  const about = mainSource.match(
    /function\s+showAboutUsageHub\(\)[\s\S]*?\n\}/,
  )?.[0] ?? "";
  for (const expected of [
    /dialog\.showMessageBox\(/,
    /buildProductIdentityPresentation\(productVersionTruth,/,
    /versionAndBuild/,
    /lifecycle/,
    /published/,
    /canary/,
  ]) {
    assert.match(about, expected);
  }
  assert.doesNotMatch(
    about,
    /Candidate · not published|候选版 · 尚未发布|Canary: not started|Canary：尚未开始/,
  );
  assert.match(
    mainSource,
    /\{\s*label:\s*"About UsageHub",\s*click:\s*showAboutUsageHub\s*\}/,
  );
  assert.doesNotMatch(mainSource, /label:\s*"About UsageHub",\s*role:\s*"about"/);
});


test("derives the three release lifecycle rows in English and Chinese", () => {
  const { buildProductIdentityPresentation } = presentationModule();
  const rows = [
    [identity(), "Candidate · Not published", "候选版 · 尚未发布"],
    [
      identity({
        releaseStage: "prerelease_ready",
        publicationStatus: "not_published",
        releaseEligible: true,
      }),
      "Pre-release ready · Not published",
      "预发布就绪 · 尚未发布",
    ],
    [
      identity({
        releaseStage: "prerelease_published",
        publicationStatus: "published_prerelease",
      }),
      "Published pre-release",
      "已发布预览版",
    ],
  ];
  for (const [value, en, zh] of rows) {
    assert.equal(buildProductIdentityPresentation(value, COPY.en).lifecycle, en);
    assert.equal(buildProductIdentityPresentation(value, COPY.zh).lifecycle, zh);
  }
});


test("derives canary clock text and safely labels unknown combinations", () => {
  const { buildProductIdentityPresentation } = presentationModule();
  for (const [canaryClock, en, zh] of [
    ["not_started", "Not started", "尚未开始"],
    ["running", "Running", "进行中"],
    ["passed", "Passed", "已通过"],
    ["blocked", "Blocked", "已阻塞"],
    ["future_state", "Unknown", "未知"],
  ]) {
    const value = identity({ canaryClock });
    assert.equal(buildProductIdentityPresentation(value, COPY.en).canary, `Canary: ${en} · 0/5`);
    assert.equal(buildProductIdentityPresentation(value, COPY.zh).canary, `Canary：${zh} · 0/5`);
  }

  const crossed = identity({
    releaseStage: "candidate",
    publicationStatus: "published_prerelease",
  });
  assert.equal(buildProductIdentityPresentation(crossed, COPY.en).lifecycle, "Unknown");
  assert.equal(buildProductIdentityPresentation(crossed, COPY.zh).lifecycle, "未知");
});


test("presentation excludes receipt and artifact digest fields", () => {
  const { buildProductIdentityPresentation } = presentationModule();
  const value = identity({
    releaseStage: "prerelease_published",
    publicationStatus: "published_prerelease",
    publicationReceipt: {
      releaseId: "private-release-id",
      sourceSha: "private-source-sha",
      manifestSha256: "private-manifest-digest",
      assetSetSha256: "private-asset-digest",
    },
  });
  for (const copy of [COPY.en, COPY.zh]) {
    assert.doesNotMatch(
      JSON.stringify(buildProductIdentityPresentation(value, copy)),
      /publicationReceipt|releaseId|sourceSha|manifestSha256|assetSetSha256|private-/,
    );
  }
});


test("About copy contains no credential or Provider payload access", () => {
  const about = mainSource.match(
    /function\s+showAboutUsageHub\(\)[\s\S]*?\n\}/,
  )?.[0] ?? "";
  assert.notEqual(about, "");
  assert.doesNotMatch(
    about,
    /api[_-]?key|bearer|cookie|credential|providerPayload|readFile|fetch\(/i,
  );
});
