import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import { messages } from "../.test-dist/i18n.js";


const apiSource = await readFile(
  new URL("../src/api.ts", import.meta.url),
  "utf8",
);
const dataHealthSource = await readFile(
  new URL("../src/pages/DataHealthPage.tsx", import.meta.url),
  "utf8",
);
const apiLoad = await loadApiModule();


async function loadApiModule() {
  try {
    let output = ts.transpileModule(apiSource, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "api.ts",
    }).outputText;
    const runtimeStub = `data:text/javascript;base64,${Buffer.from(
      "export const normalizeRuntimeCapability = value => value;",
    ).toString("base64")}`;
    const adviceStub = `data:text/javascript;base64,${Buffer.from(
      "export const canonicalShouldSendRequest = value => value; export const normalizeShouldSendAdvice = value => value;",
    ).toString("base64")}`;
    const observerPlatformUrl = new URL(
      "../.test-dist/observerPlatformCapability.js",
      import.meta.url,
    ).href;
    output = output
      .replace(
        /from\s+["']\.\/runtimeCapability(?:\.js)?["']/g,
        `from "${runtimeStub}"`,
      )
      .replace(
        /from\s+["']\.\/shouldSendAdvice(?:\.js)?["']/g,
        `from "${adviceStub}"`,
      )
      .replace(
        /["']\.\/observerPlatformCapability(?:\.js)?["']/g,
        `"${observerPlatformUrl}"`,
      );
    const module = await import(
      `data:text/javascript;base64,${Buffer.from(output).toString("base64")}`
    );
    return { module, error: null };
  } catch (error) {
    return {
      module: null,
      error: error instanceof Error ? error.message : "could not load module",
    };
  }
}


test("ships complete English and Chinese platform evidence copy", () => {
  const expected = {
    observerPlatformTitle: ["Platform source evidence", "平台数据源证据"],
    observerPlatformSupported: ["Sources verified", "数据源已验证"],
    observerPlatformUnverified: ["Not verified yet", "尚未验证"],
    observerPlatformUnknown: ["Unknown", "未知"],
    observerPlatformVerifiedCount: [
      "{supported} of {total} sources verified",
      "{total} 个数据源中 {supported} 个已验证",
    ],
    observerPlatformCountUnknown: [
      "Verified source count is unknown",
      "已验证数据源数量未知",
    ],
    platformMacOS: ["macOS", "macOS"],
    platformWindows: ["Windows", "Windows"],
    platformLinux: ["Linux", "Linux"],
    platformUnknown: ["Unknown platform", "平台未知"],
  };
  for (const [key, [english, chinese]] of Object.entries(expected)) {
    assert.equal(messages.en[key], english, `English ${key}`);
    assert.equal(messages.zh[key], chinese, `Chinese ${key}`);
  }
});


test("platform evidence copy never claims all sources or providers are healthy", () => {
  const platformKeys = Object.keys(messages.en).filter((key) =>
    key.startsWith("observerPlatform"),
  );
  const platformCopy = {
    en: platformKeys.map((key) => messages.en[key]).join("\n"),
    zh: platformKeys.map((key) => messages.zh[key]).join("\n"),
  };

  assert.doesNotMatch(
    platformCopy.en,
    /\ball\s+(?:sources|providers)\s+(?:are\s+)?(?:supported|healthy)\b/i,
  );
  assert.doesNotMatch(
    platformCopy.zh,
    /(?:所有|全部)(?:数据源|提供商|Provider)(?:均|都)?(?:已)?(?:支持|健康)/i,
  );
});


test("fetches only the normalized observerPlatform projection", () => {
  assert.match(apiSource, /fetchObserverPlatformCapability/);
  assert.match(apiSource, /\/v1\/capabilities/);
  assert.match(apiSource, /normalizeObserverPlatformCapability/);
  assert.doesNotMatch(apiSource, /observerPlatform[^\n]*(token|credential|accountRef)/i);
});


test("observer platform fetch is one credential-free GET with a closed result", async () => {
  assert.equal(
    typeof apiLoad.module?.fetchObserverPlatformCapability,
    "function",
    `api.ts must export fetchObserverPlatformCapability(): ${apiLoad.error ?? "export missing"}`,
  );
  const privateCanary = "PRIVATE_CAPABILITY_CANARY_18a2";
  const projection = {
    operatingSystem: "windows",
    support: "unsupported",
    supportedSourceCount: 0,
    totalSourceCount: 49,
    reasonCode: "source_level_evidence_unverified",
  };
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        observerPlatform: projection,
        providers: [{ secretToken: privateCanary }],
        privateRuntimePath: privateCanary,
      }),
    };
  };
  try {
    const result = await apiLoad.module.fetchObserverPlatformCapability();
    assert.deepEqual(result, projection);
    assert.doesNotMatch(JSON.stringify(result), new RegExp(privateCanary));
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1, "make exactly one renderer request");
  const [path, init = {}] = calls[0];
  assert.equal(path, "/v1/capabilities");
  assert.equal(init.method ?? "GET", "GET");
  assert.equal(init.credentials, "omit");
  for (const forbidden of ["headers", "body"]) {
    assert.ok(!(forbidden in init), `${forbidden} must not cross this boundary`);
  }
});


test("Data Health renders semantic platform evidence without raw reason codes", () => {
  assert.match(dataHealthSource, /fetchObserverPlatformCapability/);
  assert.match(dataHealthSource, /observerPlatformViewModel/);
  assert.match(dataHealthSource, /observerPlatformTitle/);
  assert.match(dataHealthSource, /supportedSourceCount/);
  assert.match(dataHealthSource, /totalSourceCount/);
  assert.match(dataHealthSource, /<dl[\s>]/);
  assert.match(dataHealthSource, /pillClass\(/);
  assert.doesNotMatch(dataHealthSource, /reasonCode/);
});


test("a platform-only last-good snapshot stays visible when Gateway health is unavailable", () => {
  assert.match(
    dataHealthSource,
    /capability\s*!==\s*null\s*\|\|\s*observerPlatform\s*!==\s*null/,
  );
});


test("initial capability loading reserves the service snapshot layout", () => {
  assert.match(
    dataHealthSource,
    /hasSnapshot:\s*capabilityLoading\s*\|\|\s*capability\s*!==\s*null\s*\|\|\s*observerPlatform\s*!==\s*null/,
  );
});
