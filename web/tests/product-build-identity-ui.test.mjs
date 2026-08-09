import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";


const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const i18nSource = await readFile(new URL("../src/i18n.ts", import.meta.url), "utf8");
const cssSource = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");

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

let presentationModule;

async function loadPresentationModule() {
  if (presentationModule) return presentationModule;
  const source = await readFile(
    new URL("../src/productBuildIdentity.ts", import.meta.url),
    "utf8",
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: "productBuildIdentity.ts",
  }).outputText;
  presentationModule = await import(
    `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
  );
  return presentationModule;
}

function identity(overrides = {}) {
  return { ...BASE_IDENTITY, ...overrides };
}


test("renders one text-complete build identity from the public projection", () => {
  assert.match(
    appSource,
    /import\s*\{[^}]*buildProductIdentityPresentation[^}]*\}\s*from\s*"\.\/productBuildIdentity"/s,
  );
  assert.match(
    appSource,
    /buildProductIdentityPresentation\(productVersionTruth,\s*t\)/,
  );
  assert.match(
    appSource,
    /<section\s+className="build-identity"\s+aria-label=\{t\.buildIdentityLabel\}>[\s\S]*?versionAndBuild[\s\S]*?lifecycle[\s\S]*?published[\s\S]*?canary[\s\S]*?<\/section>/,
  );
  assert.doesNotMatch(appSource, /t\.(?:candidateNotPublished|canaryNotStarted)/);
  assert.doesNotMatch(appSource, /className="build-identity"[^>]*role="status"/);
});


test("localizes candidate, publication, baseline, canary, and build copy", () => {
  for (const copy of [
    "Build identity",
    "构建身份",
    "Candidate",
    "候选版",
    "Not published",
    "尚未发布",
    "Pre-release ready",
    "预发布就绪",
    "Published pre-release",
    "已发布预览版",
    "Unknown",
    "未知",
    "Published stable: {version}",
    "已发布稳定版：{version}",
    "Not started",
    "尚未开始",
    "Running",
    "进行中",
    "Passed",
    "已通过",
    "Blocked",
    "已阻塞",
  ]) {
    assert.equal(i18nSource.includes(copy), true, `missing localized copy: ${copy}`);
  }
});


test("derives all three release lifecycle rows from fields in both languages", async () => {
  const { buildProductIdentityPresentation } = await loadPresentationModule();
  const rows = [
    {
      identity: identity(),
      en: "Candidate · Not published",
      zh: "候选版 · 尚未发布",
    },
    {
      identity: identity({
        releaseStage: "prerelease_ready",
        publicationStatus: "not_published",
        releaseEligible: true,
      }),
      en: "Pre-release ready · Not published",
      zh: "预发布就绪 · 尚未发布",
    },
    {
      identity: identity({
        releaseStage: "prerelease_published",
        publicationStatus: "published_prerelease",
        releaseEligible: false,
      }),
      en: "Published pre-release",
      zh: "已发布预览版",
    },
  ];

  for (const row of rows) {
    assert.equal(buildProductIdentityPresentation(row.identity, COPY.en).lifecycle, row.en);
    assert.equal(buildProductIdentityPresentation(row.identity, COPY.zh).lifecycle, row.zh);
  }
});


test("derives every canary clock and safely degrades unknown combinations", async () => {
  const { buildProductIdentityPresentation } = await loadPresentationModule();
  const clocks = [
    ["not_started", "Not started", "尚未开始"],
    ["running", "Running", "进行中"],
    ["passed", "Passed", "已通过"],
    ["blocked", "Blocked", "已阻塞"],
    ["future_state", "Unknown", "未知"],
  ];
  for (const [canaryClock, en, zh] of clocks) {
    const value = identity({ canaryClock });
    assert.equal(
      buildProductIdentityPresentation(value, COPY.en).canary,
      `Canary: ${en} · 0/5`,
    );
    assert.equal(
      buildProductIdentityPresentation(value, COPY.zh).canary,
      `Canary：${zh} · 0/5`,
    );
  }

  const crossed = identity({
    releaseStage: "prerelease_ready",
    publicationStatus: "published_prerelease",
    releaseEligible: false,
  });
  assert.equal(buildProductIdentityPresentation(crossed, COPY.en).lifecycle, "Unknown");
  assert.equal(buildProductIdentityPresentation(crossed, COPY.zh).lifecycle, "未知");
});


test("never projects publication receipt details into visible copy", async () => {
  const { buildProductIdentityPresentation } = await loadPresentationModule();
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
    const visible = JSON.stringify(buildProductIdentityPresentation(value, copy));
    assert.doesNotMatch(
      visible,
      /publicationReceipt|releaseId|sourceSha|manifestSha256|assetSetSha256|private-/,
    );
  }
});


test("keeps the identity readable without blocking 320px navigation", () => {
  assert.match(
    cssSource,
    /\.build-identity\s*\{[^}]*margin-top:\s*auto[^}]*border:/s,
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.build-identity\s*\{[^}]*flex:\s*0\s+0\s+auto[^}]*margin-top:\s*0[^}]*scroll-snap-align:\s*center/s,
  );
});


test("keeps desktop build identity in a viewport-bound sidebar", () => {
  assert.match(
    cssSource,
    /\.sidebar\s*\{[^}]*position:\s*sticky[^}]*top:\s*0[^}]*height:\s*100vh[^}]*overflow-y:\s*auto/s,
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.sidebar\s*\{[^}]*position:\s*static[^}]*height:\s*auto[^}]*overflow-y:\s*hidden[^}]*\}/s,
  );
});
