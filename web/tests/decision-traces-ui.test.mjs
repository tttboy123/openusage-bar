import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import { messages } from "../.test-dist/i18n.js";

const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const pageSource = await readFile(
  new URL("../src/pages/AutomationPage.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

async function loadApiModule() {
  let output = ts.transpileModule(apiSource, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: "api.ts",
  }).outputText;
  const replacements = [
    ["runtimeCapability", "../.test-dist/runtimeCapability.js"],
    ["shouldSendAdvice", "../.test-dist/shouldSendAdvice.js"],
    ["decisionTraces", "../.test-dist/decisionTraces.js"],
  ];
  for (const [moduleName, target] of replacements) {
    output = output.replace(
      new RegExp(`from\\s+["']\\./${moduleName}(?:\\.js)?["']`, "g"),
      `from "${new URL(target, import.meta.url).href}"`,
    );
    output = output.replace(
      new RegExp(`import\\(["']\\./${moduleName}(?:\\.js)?["']\\)`, "g"),
      `import("${new URL(target, import.meta.url).href}")`,
    );
  }
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

test("Decision Trace fetch uses one credential-free relative GET and returns only normalized data", async () => {
  const api = await loadApiModule();
  assert.equal(typeof api.fetchDecisionTraces, "function");
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        apiVersion: "gateway-decision-trace.openusage/v1",
        traces: [],
      }),
    };
  };
  try {
    assert.deepEqual(await api.fetchDecisionTraces(), {
      apiVersion: "gateway-decision-trace.openusage/v1",
      traces: [],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/gateway/v1/decision-traces");
  assert.ok(calls[0][1].signal instanceof AbortSignal);
  assert.deepEqual({ ...calls[0][1], signal: undefined }, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
    signal: undefined,
  });
  assert.match(apiSource, /normalizeDecisionTraces\(payload\)/u);
  assert.doesNotMatch(
    apiSource,
    /gateway\/v1\/decision-traces[^\n]*(?:token|credential|authorization|bearer)/iu,
  );
});

test("Decision Trace keeps its intentional three-second freshness cap", async () => {
  const api = await loadApiModule();
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const scheduledDelays = [];
  const clearedHandles = [];
  const timeoutHandle = Object.freeze({ kind: "decision-trace-timeout" });
  let observedRequest = null;

  globalThis.setTimeout = (callback, delay) => {
    scheduledDelays.push(delay);
    queueMicrotask(callback);
    return timeoutHandle;
  };
  globalThis.clearTimeout = (handle) => {
    clearedHandles.push(handle);
  };
  globalThis.fetch = async (path, init) => {
    observedRequest = { path, init };
    assert.ok(init?.signal instanceof AbortSignal, "request must own an AbortSignal");
    return new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason), {
        once: true,
      });
    });
  };

  try {
    await assert.rejects(api.fetchDecisionTraces(), { name: "AbortError" });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }

  assert.equal(observedRequest?.path, "/gateway/v1/decision-traces");
  assert.equal(observedRequest?.init?.method, "GET");
  assert.equal(observedRequest?.init?.credentials, "omit");
  assert.deepEqual(scheduledDelays, [3_000]);
  assert.deepEqual(clearedHandles, [timeoutHandle]);
  assert.deepEqual(
    Object.keys(observedRequest?.init ?? {}).sort(),
    ["cache", "credentials", "method", "signal"],
  );
  assert.equal(observedRequest?.init?.headers, undefined);
  assert.equal(observedRequest?.init?.body, undefined);
});

test("Decision Trace fetch reports an invalid projection as unknown without reflection", async () => {
  const api = await loadApiModule();
  const canary = "RAW_PROMPT_TOKEN_CANARY_83fa";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      apiVersion: "gateway-decision-trace.openusage/v1",
      traces: [],
      prompt: canary,
    }),
  });
  try {
    const result = await api.fetchDecisionTraces();
    assert.equal(result, null);
    assert.doesNotMatch(JSON.stringify(result), /CANARY/u);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Automation gives loading, empty, unavailable, and invalid-contract states distinct copy", () => {
  const copy = {
    decisionTraceTitle: ["Recent Decision Trace", "最近决策轨迹"],
    decisionTraceRuntimeOnly: [
      "Decision traces stay only in the current Gateway process memory; restarting clears them. They are not saved as history.",
      "仅保存在当前 Gateway 进程内存中；重启会清空这些轨迹，不会保存为历史记录。",
    ],
    decisionTraceLoading: ["Loading recent decisions…", "正在加载最近决策…"],
    decisionTraceRefreshing: [
      "Refreshing recent decisions… Showing the last loaded decisions.",
      "正在刷新最近决策…当前显示上次加载的决策。",
    ],
    decisionTraceRefreshFailed: [
      "Could not refresh Decision Trace. Showing the last loaded decisions.",
      "无法刷新决策轨迹；正在显示上次加载的决策。",
    ],
    decisionTraceEmpty: [
      "No decisions have been recorded in this runtime yet.",
      "当前运行期间尚未记录决策。",
    ],
    decisionTraceUnavailable: [
      "Decision Trace is unavailable. The optional Gateway host may be offline or unsupported.",
      "决策轨迹不可用；可选 Gateway host 可能离线或不支持此能力。",
    ],
    decisionTraceUnknown: [
      "Decision Trace returned an unrecognized contract. No details are shown.",
      "决策轨迹返回了无法识别的合同；未显示任何详情。",
    ],
    decisionTraceAdviceOnly: [
      "Advice only — no request was executed.",
      "仅建议——未执行请求。",
    ],
    decisionTraceExecuted: ["Executed by UsageHub", "由 UsageHub 执行"],
    decisionTraceRefresh: ["Refresh decisions", "刷新决策"],
  };
  for (const [key, [en, zh]] of Object.entries(copy)) {
    assert.equal(messages.en[key], en, `English ${key}`);
    assert.equal(messages.zh[key], zh, `Chinese ${key}`);
    assert.match(pageSource, new RegExp(`t\\.${key}\\b`, "u"), key);
  }
  assert.doesNotMatch(messages.en.decisionTraceEmpty, /unavailable|offline/iu);
  assert.doesNotMatch(messages.zh.decisionTraceEmpty, /不可用|离线/u);
});

test("Automation renders Decision Trace as an accessible in-page timeline with safe optional facts", () => {
  assert.match(pageSource, /fetchDecisionTraces/u);
  assert.match(pageSource, /aria-labelledby="decision-trace-title"/u);
  assert.match(pageSource, /className="decision-trace-list"/u);
  assert.match(pageSource, /<time\s+dateTime=/u);
  assert.match(pageSource, /role="status"/u);
  assert.match(pageSource, /aria-live="polite"/u);
  assert.match(pageSource, /aria-busy=\{decisionTraceBusy\}/u);
  assert.match(pageSource, /type="button"[\s\S]*?decisionTraceRefresh/u);
  assert.match(pageSource, /trace\.pool\s*!==\s*null/u);
  assert.match(pageSource, /trace\.selected\s*!==\s*null/u);
  assert.match(pageSource, /trace\.fallback\s*!==\s*null/u);
  assert.match(pageSource, /trace\.exclusions\.length\s*>\s*0/u);
  assert.match(pageSource, /trace\.factsWindow\s*!==\s*null/u);
  assert.doesNotMatch(
    pageSource,
    /trace\.(?:prompt|response|credential|token|header|path|endpoint|rawError|accountId)\b/u,
  );
  assert.doesNotMatch(pageSource, /dangerouslySetInnerHTML/u);
});

test("Decision Trace layout stays bounded at 320, 768, and 1440 pixels", () => {
  assert.match(cssSource, /\.decision-trace-card\s*\{[^}]*min-width:\s*0/isu);
  assert.match(cssSource, /\.decision-trace-item\s*\{[^}]*min-width:\s*0/isu);
  assert.match(cssSource, /\.decision-trace-value[^}]*overflow-wrap:\s*anywhere/isu);
  assert.match(cssSource, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.decision-trace-head/isu);
  assert.doesNotMatch(
    cssSource.match(/\.decision-trace-card[\s\S]*?(?=\n\.[a-z]|\n@media)/iu)?.[0] ?? "",
    /overflow-x:\s*(?:auto|scroll)/iu,
  );
});
