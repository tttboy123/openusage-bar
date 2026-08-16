import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import { pluginHealthViewState } from "../.test-dist/pluginHealthViewState.js";
import { messages } from "../.test-dist/i18n.js";

const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const pageSource = await readFile(
  new URL("../src/pages/DataHealthPage.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

async function loadApiModule() {
  let output = ts.transpileModule(apiSource, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
    fileName: "api.ts",
  }).outputText;
  for (const [moduleName, target] of [
    ["runtimeCapability", "../.test-dist/runtimeCapability.js"],
    ["shouldSendAdvice", "../.test-dist/shouldSendAdvice.js"],
    ["decisionTraces", "../.test-dist/decisionTraces.js"],
    ["pluginConnections", "../.test-dist/pluginConnections.js"],
  ]) {
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

test("plugin connections use the single credential-free host GET and normalize its response", async () => {
  const api = await loadApiModule();
  assert.equal(typeof api.fetchPluginConnections, "function");
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        apiVersion: "plugin.openusage/v1",
        object: "plugin.connections",
        observedAt: "2026-08-10T03:04:05.123456Z",
        connections: [
          {
            pluginId: "loom",
            configuration: "configured",
            connection: "connected",
            capabilityState: "negotiated",
            capabilities: [
              "decision.lookup",
              "health.query",
              "outcome.record",
              "quotas.query",
              "route.advice",
              "usage.query",
            ],
            lastSeenAt: "2026-08-10T03:04:05.123456Z",
            lastSyncOutcome: "succeeded",
            lastSyncAt: "2026-08-10T03:04:05.123456Z",
          },
          {
            pluginId: "codex",
            configuration: "not_configured",
            connection: "never_seen",
            capabilityState: "not_negotiated",
            capabilities: [],
            lastSeenAt: null,
            lastSyncOutcome: "never",
            lastSyncAt: null,
          },
          {
            pluginId: "claude_code",
            configuration: "unknown",
            connection: "unknown",
            capabilityState: "unknown",
            capabilities: [],
            lastSeenAt: null,
            lastSyncOutcome: "unknown",
            lastSyncAt: null,
          },
        ],
      }),
    };
  };
  try {
    assert.notEqual(await api.fetchPluginConnections(), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls.length, 1);
  const [path, init] = calls[0];
  assert.equal(path, "/host/v1/plugin-connections");
  assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual({ ...init, signal: undefined }, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
    signal: undefined,
  });
});

test("plugin connections abort a stalled credential-free host request after five seconds", async () => {
  const api = await loadApiModule();
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const scheduledDelays = [];
  const clearedHandles = [];
  const timeoutHandles = [];
  const observedRequests = [];

  globalThis.setTimeout = (callback, delay) => {
    scheduledDelays.push(delay);
    queueMicrotask(callback);
    const handle = Object.freeze({
      kind: "plugin-connections-timeout",
      index: timeoutHandles.length,
    });
    timeoutHandles.push(handle);
    return handle;
  };
  globalThis.clearTimeout = (handle) => {
    clearedHandles.push(handle);
  };
  globalThis.fetch = async (path, init) => {
    observedRequests.push({ path, init });
    assert.ok(
      init?.signal instanceof AbortSignal,
      "request must own an AbortSignal",
    );
    return new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason), {
        once: true,
      });
    });
  };

  try {
    const first = api.fetchPluginConnections();
    const second = api.fetchPluginConnections();
    await Promise.all([
      assert.rejects(first, { name: "AbortError" }),
      assert.rejects(second, { name: "AbortError" }),
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }

  assert.equal(observedRequests.length, 2);
  assert.notEqual(observedRequests[0].init.signal, observedRequests[1].init.signal);
  for (const observedRequest of observedRequests) {
    assert.equal(observedRequest.path, "/host/v1/plugin-connections");
    assert.equal(observedRequest.init.credentials, "omit");
    assert.equal(observedRequest.init.headers, undefined);
    assert.equal(observedRequest.init.body, undefined);
  }
  assert.deepEqual(scheduledDelays, [5_000, 5_000]);
  assert.deepEqual(clearedHandles, timeoutHandles);
});

test("plugin health owns checking, unavailable, refreshing, last-known, and ready states", () => {
  const cases = [
    [{ checking: true, failed: false, hasSnapshot: false }, "checking", false],
    [{ checking: false, failed: true, hasSnapshot: false }, "initial_unavailable", false],
    [{ checking: true, failed: false, hasSnapshot: true }, "refreshing", true],
    [{ checking: false, failed: true, hasSnapshot: true }, "last_known", true],
    [{ checking: false, failed: false, hasSnapshot: true }, "ready", true],
  ];
  for (const [input, state, showSnapshot] of cases) {
    const model = pluginHealthViewState(input);
    assert.equal(model.state, state);
    assert.equal(model.showSnapshot, showSnapshot);
    assert.equal(model.actionBusy, state === "checking" || state === "refreshing");
    assert.equal(model.actionDisabled, model.actionBusy);
  }

  const canary = "PRIVATE_TOKEN_PATH_ERROR_CANARY";
  assert.doesNotMatch(JSON.stringify(pluginHealthViewState({
    checking: false,
    failed: true,
    hasSnapshot: false,
    token: canary,
    path: canary,
    error: canary,
  })), /CANARY/u);
});

test("plugin health copy keeps configuration, connection, and sync facts explicit in both languages", () => {
  const copy = {
    pluginConnectionsTitle: ["Optional integrations", "可选集成"],
    pluginConnectionsHint: [
      "Optional integrations do not affect Observer or Gateway. Configuration and connection do not prove Provider health or a successful sync.",
      "可选集成状态不影响 Observer 或 Gateway；已配置或已连接不代表 Provider 健康或同步成功。",
    ],
    pluginConnectionsChecking: ["Checking integration status…", "正在检查集成状态…"],
    pluginConnectionsRefreshing: ["Refreshing integration status…", "正在刷新集成状态…"],
    pluginConnectionsUnavailable: [
      "Integration status is unavailable. The desktop host may be offline or unsupported.",
      "集成状态不可用；桌面 host 可能离线或不支持此能力。",
    ],
    pluginConnectionsLastKnown: [
      "Integration status could not be refreshed. Showing the last known status.",
      "无法刷新集成状态，当前显示最近一次已知状态。",
    ],
    pluginConfiguration: ["Configuration", "配置状态"],
    pluginConnection: ["Connection", "连接状态"],
    pluginCapabilities: ["Capabilities", "协商能力"],
    pluginRecentSync: ["Recent sync", "最近同步"],
    pluginConfigurationConfigured: ["Configured", "已配置"],
    pluginConfigurationNotConfigured: ["Not configured", "未配置"],
    pluginConfigurationUnknown: ["Configuration unknown", "配置状态未知"],
    pluginConnectionNeverSeen: ["Never seen", "从未连接"],
    pluginConnectionConnected: ["Connected", "已连接"],
    pluginConnectionStale: ["Stale", "连接已过期"],
    pluginConnectionUnknown: ["Connection unknown", "连接状态未知"],
    pluginSyncNever: ["Never synced", "从未同步"],
    pluginSyncSucceeded: ["Succeeded", "同步成功"],
    pluginSyncFailed: ["Failed", "同步失败"],
    pluginSyncUnknown: ["Sync history unknown", "同步历史未知"],
  };
  for (const [key, [en, zh]] of Object.entries(copy)) {
    assert.equal(messages.en[key], en, `English ${key}`);
    assert.equal(messages.zh[key], zh, `Chinese ${key}`);
  }
  assert.notEqual(messages.en.pluginSyncNever, messages.en.pluginSyncUnknown);
  assert.notEqual(messages.zh.pluginSyncNever, messages.zh.pluginSyncUnknown);
});

test("Data Health renders plugin facts in an independent accessible in-page panel", () => {
  assert.match(pageSource, /fetchPluginConnections/u);
  assert.match(pageSource, /pluginHealthViewState/u);
  assert.match(pageSource, /aria-labelledby="plugin-connections-title"/u);
  assert.match(pageSource, /aria-describedby="plugin-connections-hint"/u);
  assert.match(pageSource, /className="plugin-connection-list"/u);
  assert.match(pageSource, /<time\s+dateTime=/u);
  assert.match(pageSource, /aria-live="polite"/u);
  assert.match(pageSource, /aria-atomic="true"/u);
  assert.match(pageSource, /pluginStatus\.announcementKey/u);
  assert.match(pageSource, /pluginStatus\.actionDisabled/u);
  assert.match(pageSource, /pluginStatus\.actionBusy/u);
  assert.match(pageSource, /const activeElement = document\.activeElement/u);
  assert.match(
    pageSource,
    /activeElement === pluginActionRef\.current[\s\S]*?activeElement === document\.body/u,
  );
  assert.ok(
    pageSource.indexOf("plugin-connections-title") < pageSource.indexOf("observer-sources-title"),
    "optional integrations precede Observer sources",
  );
  const issueCount = pageSource.match(/const issueCount[\s\S]*?;/u)?.[0] ?? "";
  assert.doesNotMatch(issueCount, /plugin|connection/iu);
  assert.doesNotMatch(
    pageSource,
    /plugin\.(?:token|credential|header|path|endpoint|rawError|accountId)|dangerouslySetInnerHTML/iu,
  );
});

test("plugin health layout stays bounded at 320, 768, and 1440 pixels", () => {
  assert.match(cssSource, /\.plugin-connections-panel\s*\{[^}]*min-width:\s*0/isu);
  assert.match(cssSource, /\.plugin-connection-item\s*\{[^}]*min-width:\s*0/isu);
  assert.match(cssSource, /\.plugin-connection-value[^}]*overflow-wrap:\s*anywhere/isu);
  assert.match(cssSource, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.plugin-connection-facts/isu);
  assert.match(cssSource, /@media\s*\(forced-colors:\s*active\)[\s\S]*?\.plugin-connection/isu);
  assert.match(cssSource, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.plugin-connection/isu);
  assert.doesNotMatch(
    cssSource.match(/\.plugin-connections-panel[\s\S]*?(?=\n\.[a-z]|\n@media)/iu)?.[0] ?? "",
    /overflow-x:\s*(?:auto|scroll)/iu,
  );
});
