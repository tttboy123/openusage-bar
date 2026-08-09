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
const pageFile = ts.createSourceFile(
  "AutomationPage.tsx",
  pageSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);
const adviceLoad = await loadAdviceModule();
const apiLoad = await loadApiModule(adviceLoad.url);

const COPY = {
  shouldSendTitle: ["Should-Send advice", "Should-Send 建议"],
  shouldSendHint: [
    "Check recorded local facts before sending. This does not forward a request.",
    "发送前检查已记录的本地事实；此操作不会转发请求。",
  ],
  shouldSendProvider: ["Provider", "Provider"],
  shouldSendModel: ["Model", "模型"],
  shouldSendEstimatedTokens: ["Estimated Tokens", "预计 Token"],
  shouldSendWindow: ["Window", "时间窗口"],
  shouldSendCheck: ["Check advice", "查看建议"],
  shouldSendChecking: ["Checking advice…", "正在查看建议…"],
  shouldSendDecisionYes: ["Send", "可以发送"],
  shouldSendDecisionNo: ["Do not send", "不要发送"],
  shouldSendDecisionDefer: ["Wait and check again", "暂缓并稍后重试"],
  shouldSendDecisionUnknown: ["Advice unavailable", "建议不可用"],
  shouldSendRetry: ["Retry advice", "重试建议"],
  shouldSendDeferTimingTitle: ["When to check again", "再次查看时间"],
  shouldSendDeferLocalTime: ["Local time", "本地时间"],
  shouldSendDeferInMinutes: [
    "Check again in about {count} min.",
    "约 {count} 分钟后再次查看。",
  ],
  shouldSendDeferInHours: [
    "Check again in about {count} hr.",
    "约 {count} 小时后再次查看。",
  ],
  shouldSendDeferInDays: [
    "Check again in about {count} days.",
    "约 {count} 天后再次查看。",
  ],
  shouldSendDeferReady: [
    "You can check again now.",
    "现在可以再次查看。",
  ],
  shouldSendDeferAfterRefresh: [
    "Check again after local usage data refreshes.",
    "请在本地用量数据刷新后再次查看。",
  ],
  shouldSendLocalFactsInsufficient: [
    "Insufficient local facts",
    "本地事实不足",
  ],
};

function descendants(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}

function jsxElements(tagName) {
  return descendants(
    pageFile,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(pageFile) === tagName,
  );
}

function adviceModule() {
  assert.ok(
    adviceLoad.module,
    `src/shouldSendAdvice.ts must provide the pure advice seam: ${adviceLoad.error}`,
  );
  return adviceLoad.module;
}

async function loadAdviceModule() {
  try {
    const source = await readFile(
      new URL("../src/shouldSendAdvice.ts", import.meta.url),
      "utf8",
    );
    const output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "shouldSendAdvice.ts",
    }).outputText;
    const url = `data:text/javascript;base64,${Buffer.from(output).toString("base64")}`;
    return { module: await import(url), url, error: null };
  } catch (error) {
    return {
      module: null,
      url: null,
      error: error instanceof Error ? error.message : "could not load module",
    };
  }
}

async function loadApiModule(adviceUrl) {
  try {
    let output = ts.transpileModule(apiSource, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "api.ts",
    }).outputText;
    output = output.replace(
      /from\s+["']\.\/runtimeCapability(?:\.js)?["']/g,
      `from "${new URL("../.test-dist/runtimeCapability.js", import.meta.url).href}"`,
    );
    if (adviceUrl) {
      output = output.replace(
        /from\s+["']\.\/shouldSendAdvice(?:\.js)?["']/g,
        `from "${adviceUrl}"`,
      );
    }
    return {
      module: await import(
        `data:text/javascript;base64,${Buffer.from(output).toString("base64")}`
      ),
      error: null,
    };
  } catch (error) {
    return {
      module: null,
      error: error instanceof Error ? error.message : "could not load module",
    };
  }
}

test("Should-Send uses one bounded relative credential-free POST and validates every field", async () => {
  assert.equal(
    typeof apiLoad.module?.fetchShouldSendAdvice,
    "function",
    `api.ts must export fetchShouldSendAdvice(request): ${apiLoad.error ?? "export missing"}`,
  );
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        decision: "yes",
        confidence: 0.92,
        reason: "quota_healthy",
        defer_until: null,
        details: {
          quota_remaining: 850,
          burn_rate_per_min: 10,
          predicted_exhaustion_minutes: 85,
        },
      }),
    };
  };
  try {
    await apiLoad.module.fetchShouldSendAdvice({
      provider: "openai",
      model: "gpt-4o",
      estimated_tokens: 8000,
      window: "5m",
    });
    const invalid = [
      { provider: " ", model: "gpt-4o", estimated_tokens: 1, window: "5m" },
      { provider: "openai", model: "x".repeat(257), estimated_tokens: 1, window: "5m" },
      { provider: "openai", model: "gpt-4o", estimated_tokens: 0, window: "5m" },
      { provider: "openai", model: "gpt-4o", estimated_tokens: true, window: "5m" },
      { provider: "openai", model: "gpt-4o", estimated_tokens: 1, window: "" },
      { provider: "openai", model: "gpt-4o", estimated_tokens: 1, window: "5m", token: "secret" },
    ];
    for (const value of invalid) {
      await assert.rejects(() => apiLoad.module.fetchShouldSendAdvice(value));
    }
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1, "invalid input must fail before fetch");
  const [path, init] = calls[0];
  assert.equal(path, "/gateway/v1/should-send");
  assert.equal(init.method, "POST");
  assert.equal(init.credentials, "omit");
  assert.equal(init.headers?.["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), {
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 8000,
    window: "5m",
  });
  assert.ok(new TextEncoder().encode(init.body).byteLength <= 1024);
  assert.doesNotMatch(apiSource, /authorization|bearer|gatewayToken|localhost|127\.0\.0\.1/i);
  assert.match(apiSource, /canonicalShouldSendRequest\(request\)/);
  assert.match(apiSource, /normalizeShouldSendAdvice\(payload\)/);
  assert.doesNotMatch(
    apiSource,
    /canonicalShouldSendBody|safeShouldSendAdvice|SHOULD_SEND_(?:DECISIONS|REASONS)/,
    "api.ts must delegate both Should-Send boundaries to the pure model",
  );
});

test("fetchShouldSendAdvice keeps Defer when defer_until is missing or unsafe", async () => {
  const module = adviceModule();
  const canary = "RAW_DEFER_PATH_TOKEN_API_8f3c";
  const base = {
    decision: "defer",
    confidence: 0.8,
    reason: "quota_low",
    details: {
      quota_remaining: 42,
      burn_rate_per_min: 2,
      predicted_exhaustion_minutes: 21,
    },
  };
  const responses = [
    { ...base },
    { ...base, defer_until: canary, raw_error: canary },
    { ...base, defer_until: "2026-08-09T10:15:00Z" },
  ];
  const request = {
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 8000,
    window: "5m",
  };
  let responseIndex = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    const response = responses[responseIndex++];
    return {
      ok: true,
      status: 200,
      json: async () => response,
    };
  };

  try {
    const missing = await apiLoad.module.fetchShouldSendAdvice(request);
    assert.equal(missing.decision, "defer");
    assert.equal(missing.deferUntil, null);

    const unsafe = await apiLoad.module.fetchShouldSendAdvice(request);
    assert.equal(unsafe.decision, "defer");
    assert.equal(unsafe.deferUntil, null);
    assert.deepEqual(
      module.shouldSendAdviceViewModel(
        unsafe,
        Date.parse("2026-08-09T10:00:00Z"),
      ).deferTiming,
      {
        state: "after_refresh",
        at: null,
        relativeKey: "shouldSendDeferAfterRefresh",
        relativeCount: null,
      },
    );
    assert.doesNotMatch(JSON.stringify(unsafe), new RegExp(canary));

    const scheduled = await apiLoad.module.fetchShouldSendAdvice(request);
    assert.equal(scheduled.decision, "defer");
    assert.equal(scheduled.deferUntil, "2026-08-09T10:15:00Z");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(responseIndex, responses.length);
});

test("fetchShouldSendAdvice drops defer_until from Yes and No advice", async () => {
  const module = adviceModule();
  const responses = [
    { decision: "yes", reason: "quota_healthy" },
    { decision: "no", reason: "approaching_limit" },
  ].map((value) => ({
    ...value,
    confidence: 0.8,
    defer_until: "2026-08-09T10:15:00Z",
    details: {
      quota_remaining: 42,
      burn_rate_per_min: 2,
      predicted_exhaustion_minutes: 21,
    },
  }));
  const request = {
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 8000,
    window: "5m",
  };
  let responseIndex = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    const response = responses[responseIndex++];
    return {
      ok: true,
      status: 200,
      json: async () => response,
    };
  };

  try {
    for (const decision of ["yes", "no"]) {
      const advice = await apiLoad.module.fetchShouldSendAdvice(request);
      assert.equal(advice.decision, decision);
      assert.equal(advice.deferUntil, null);
      assert.equal(
        module.shouldSendAdviceViewModel(
          advice,
          Date.parse("2026-08-09T10:00:00Z"),
        ).deferTiming,
        null,
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(responseIndex, responses.length);
});

test("fetchShouldSendAdvice keeps invalid core advice entirely Unknown", async () => {
  const canary = "RAW_INVALID_ADVICE_API_22d9";
  const base = {
    decision: "defer",
    confidence: 0.8,
    reason: "quota_low",
    defer_until: null,
    details: {
      quota_remaining: 42,
      burn_rate_per_min: 2,
      predicted_exhaustion_minutes: 21,
    },
  };
  const responses = [
    { ...base, decision: "maybe" },
    { ...base, reason: canary },
    { ...base, confidence: 1.1 },
    {
      ...base,
      details: {
        ...base.details,
        predicted_exhaustion_minutes: canary,
      },
    },
  ];
  const request = {
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 8000,
    window: "5m",
  };
  const unknown = {
    decision: "unknown",
    reason: "unknown",
    confidence: null,
    deferUntil: null,
    details: {
      quotaRemaining: null,
      burnRatePerMinute: null,
      predictedExhaustionMinutes: null,
    },
  };
  let responseIndex = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    const response = responses[responseIndex++];
    return {
      ok: true,
      status: 200,
      json: async () => response,
    };
  };

  try {
    for (const _response of responses) {
      const advice = await apiLoad.module.fetchShouldSendAdvice(request);
      assert.deepEqual(advice, unknown);
      assert.doesNotMatch(JSON.stringify(advice), new RegExp(canary));
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(responseIndex, responses.length);
});

test("pure advice model allowlists decisions, reasons, details, and null facts", () => {
  const module = adviceModule();
  assert.equal(typeof module.normalizeShouldSendAdvice, "function");
  assert.equal(typeof module.shouldSendAdviceViewModel, "function");

  const canary = "RAW_ERROR_TOKEN_PATH_CREDENTIAL_1ec7";
  const safe = module.normalizeShouldSendAdvice({
    decision: "defer",
    confidence: 0.8,
    reason: "quota_low",
    raw_error: canary,
    token: canary,
    path: canary,
    credential: canary,
    details: {
      quota_remaining: null,
      burn_rate_per_min: 10,
      predicted_exhaustion_minutes: null,
      unknown: canary,
    },
  });
  assert.doesNotMatch(JSON.stringify(safe), new RegExp(canary));
  const view = module.shouldSendAdviceViewModel(safe);
  assert.equal(view.state, "defer");
  assert.equal(view.statusKey, "shouldSendDecisionDefer");
  assert.equal(view.quotaRemainingKey, "shouldSendLocalFactsInsufficient");
  assert.equal(view.predictedExhaustionKey, "shouldSendLocalFactsInsufficient");
  assert.doesNotMatch(JSON.stringify(view), /deadline|(?:^|\D)0(?:\D|$)/i);

  for (const [decision, statusKey] of [
    ["yes", "shouldSendDecisionYes"],
    ["no", "shouldSendDecisionNo"],
    ["defer", "shouldSendDecisionDefer"],
  ]) {
    const normalized = module.normalizeShouldSendAdvice({
      decision,
      confidence: 0.8,
      reason: decision === "yes" ? "quota_healthy" : "approaching_limit",
      defer_until: null,
      details: {
        quota_remaining: 42,
        burn_rate_per_min: 2,
        predicted_exhaustion_minutes: 21,
      },
    });
    assert.equal(module.shouldSendAdviceViewModel(normalized).statusKey, statusKey);
  }
  const unknown = module.normalizeShouldSendAdvice({
    decision: "yes",
    confidence: 1,
    reason: canary,
    details: {},
  });
  assert.equal(module.shouldSendAdviceViewModel(unknown).state, "unknown");
  assert.doesNotMatch(JSON.stringify(unknown), new RegExp(canary));
});

test("Defer exposes only a closed safe retry-timing model", () => {
  const module = adviceModule();
  const now = Date.parse("2026-08-09T10:00:00Z");
  const normalized = (decision, deferUntil) =>
    module.normalizeShouldSendAdvice({
      decision,
      confidence: 0.8,
      reason: decision === "yes" ? "quota_healthy" : "quota_low",
      defer_until: deferUntil,
      details: {
        quota_remaining: 42,
        burn_rate_per_min: 2,
        predicted_exhaustion_minutes: 21,
      },
    });

  const cases = [
    {
      value: normalized("defer", "2026-08-09T10:15:00Z"),
      expected: {
        state: "scheduled",
        at: "2026-08-09T10:15:00Z",
        relativeKey: "shouldSendDeferInMinutes",
        relativeCount: 15,
      },
    },
    {
      value: normalized("defer", "2026-08-09T12:00:00Z"),
      expected: {
        state: "scheduled",
        at: "2026-08-09T12:00:00Z",
        relativeKey: "shouldSendDeferInHours",
        relativeCount: 2,
      },
    },
    {
      value: normalized("defer", "2026-08-11T11:00:00Z"),
      expected: {
        state: "scheduled",
        at: "2026-08-11T11:00:00Z",
        relativeKey: "shouldSendDeferInDays",
        relativeCount: 3,
      },
    },
    {
      value: normalized("defer", "2026-08-09T09:59:59Z"),
      expected: {
        state: "ready",
        at: "2026-08-09T09:59:59Z",
        relativeKey: "shouldSendDeferReady",
        relativeCount: null,
      },
    },
    {
      value: normalized("defer", null),
      expected: {
        state: "after_refresh",
        at: null,
        relativeKey: "shouldSendDeferAfterRefresh",
        relativeCount: null,
      },
    },
  ];

  for (const { value, expected } of cases) {
    assert.deepEqual(module.shouldSendAdviceViewModel(value, now).deferTiming, expected);
  }

  const privateCanary = "RAW_DEFER_PATH_TOKEN_87e2";
  const forged = {
    ...normalized("defer", null),
    deferUntil: privateCanary,
  };
  assert.deepEqual(module.shouldSendAdviceViewModel(forged, now).deferTiming, {
    state: "after_refresh",
    at: null,
    relativeKey: "shouldSendDeferAfterRefresh",
    relativeCount: null,
  });
  assert.doesNotMatch(
    JSON.stringify(module.shouldSendAdviceViewModel(forged, now)),
    new RegExp(privateCanary),
  );

  for (const decision of ["yes", "no"]) {
    assert.equal(
      module.shouldSendAdviceViewModel(
        normalized(decision, "2026-08-09T10:15:00Z"),
        now,
      ).deferTiming,
      null,
      `${decision} must not inherit defer timing`,
    );
  }
  assert.equal(
    module.shouldSendAdviceViewModel(
      module.normalizeShouldSendAdvice({}),
      now,
    ).deferTiming,
    null,
    "Unknown must not invent defer timing",
  );
});

test("Defer timestamps use strict Gregorian calendar semantics", () => {
  const module = adviceModule();
  const normalize = (deferUntil) =>
    module.normalizeShouldSendAdvice({
      decision: "defer",
      confidence: 0.8,
      reason: "quota_low",
      defer_until: deferUntil,
      details: {
        quota_remaining: 42,
        burn_rate_per_min: 2,
        predicted_exhaustion_minutes: 21,
      },
    });

  for (const timestamp of [
    "0001-01-01T00:00:00Z",
    "2024-02-29T10:15Z",
    "2026-01-01T10:15+08:00",
    "2026-01-01T10:15:30.1Z",
    "2026-01-01T10:15:30.123456-05:30",
  ]) {
    assert.equal(normalize(timestamp).deferUntil, timestamp, timestamp);
  }

  for (const timestamp of [
    "0000-01-01T10:15:00Z",
    "1900-02-29T10:15:00Z",
    "2026-02-30T10:15:00Z",
    "2026-01-01T24:00:00Z",
    "2026-01-01T10:15:60Z",
    "2026-01-01T10:15:00+24:00",
    "2026-01-01T10:15:30.1234567Z",
  ]) {
    const advice = normalize(timestamp);
    assert.equal(advice.decision, "defer", timestamp);
    assert.equal(advice.deferUntil, null, timestamp);
    assert.equal(
      module.shouldSendAdviceViewModel(advice).deferTiming.state,
      "after_refresh",
      timestamp,
    );
  }
});

test("Automation nests an explicit read-only advice form inside Optional Gateway", () => {
  const gateway = jsxElements("section").find((node) =>
    node.getText(pageFile).includes("t.gatewayTitle"),
  );
  assert.ok(gateway, "keep the Optional Gateway region");
  const text = gateway.getText(pageFile);
  assert.match(text, /t\.shouldSendTitle/);
  assert.match(text, /<form\b[^>]*onSubmit=/);
  assert.match(text, /fetchShouldSendAdvice/);
  assert.match(text, /type=["']submit["']/);
  const adviceResult = jsxElements("div").find((node) =>
    node.openingElement.getText(pageFile).includes("should-send-result"),
  );
  assert.ok(adviceResult, "keep a visible Should-Send result container");
  assert.doesNotMatch(
    adviceResult.openingElement.getText(pageFile),
    /\brole=|\baria-live=|\baria-atomic=/,
    "the visual advice result must not create a second live region",
  );
  for (const key of [
    "shouldSendChecking",
    "shouldSendDecisionYes",
    "shouldSendDecisionNo",
    "shouldSendDecisionDefer",
    "shouldSendDecisionUnknown",
    "shouldSendRetry",
  ]) {
    assert.match(text, new RegExp(`t\\.${key}\\b|t\\[[^\\]]*${key}`), key);
  }
  assert.doesNotMatch(
    text,
    /enableGateway|disableGateway|clearGatewayCache|clear_cache|\.message\b|\.stack\b/i,
  );
  const effects = descendants(
    pageFile,
    (node) => ts.isCallExpression(node) && node.expression.getText(pageFile) === "useEffect",
  );
  for (const effect of effects) {
    assert.doesNotMatch(
      effect.getText(pageFile),
      /fetchShouldSendAdvice/,
      "advice must run only after the user's submit/retry action",
    );
  }
});

test("Automation explains Defer with one safe absolute and relative timing block", () => {
  const deferPanel = jsxElements("div").find((node) =>
    node.openingElement.getText(pageFile).includes("should-send-defer-timing"),
  );
  assert.ok(deferPanel, "render a dedicated read-only Defer timing block");
  const panelText = deferPanel.getText(pageFile);
  assert.match(
    pageSource,
    /advice\.state\s*===\s*["']defer["']\s*&&\s*advice\.deferTiming/,
    "Yes, No, and Unknown must not render Defer timing",
  );
  assert.match(panelText, /t\.shouldSendDeferTimingTitle/);
  assert.match(panelText, /t\.shouldSendDeferLocalTime/);
  assert.match(panelText, /deferRelativeText\s*\(/);

  const times = descendants(
    deferPanel,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(pageFile) === "time",
  );
  assert.equal(times.length, 1, "a valid Defer timestamp uses semantic time markup");
  assert.match(
    times[0].openingElement.getText(pageFile),
    /dateTime=\{advice\.deferTiming\.at\}/,
  );
  assert.match(times[0].getText(pageFile), /formatDeferUntil\s*\(/);
  assert.doesNotMatch(
    panelText,
    /\brole=|\baria-live=|\baria-atomic=/,
    "the visual timing block must not create a second live region",
  );

  assert.match(pageSource, /new\s+Intl\.DateTimeFormat\s*\(/);
  assert.match(pageSource, /t\s*===\s*messages\.zh\s*\?\s*["']zh-CN["']/);
  assert.match(
    pageSource,
    /const\s+adviceAnnouncement\s*=[\s\S]*deferTimingAnnouncement\s*\(/,
    "the existing shared announcer includes Defer timing",
  );
  assert.doesNotMatch(pageSource, /advice\.deferUntil/);
});

test("Automation gates advice on capability state and cannot show stale results", () => {
  const availability = descendants(
    pageFile,
    (node) =>
      ts.isFunctionDeclaration(node) &&
      node.name?.getText(pageFile) === "shouldSendAvailability",
  )[0];
  assert.ok(availability, "keep capability availability as one explicit policy");
  const availabilityText = availability.getText(pageFile);
  assert.match(availabilityText, /gateway\.mode\s*===\s*["']observe["']/);
  assert.match(availabilityText, /gateway\.operational\s*===\s*["']starting["']/);
  assert.match(availabilityText, /gateway\.operational\s*!==\s*["']ready["']/);
  assert.match(availabilityText, /gateway\.operational\s*!==\s*["']degraded["']/);
  assert.match(availabilityText, /feature\.support\s*!==\s*["']supported["']/);
  assert.match(availabilityText, /feature\.enabled\s*!==\s*true/);
  assert.match(availabilityText, /feature\.configured\s*!==\s*true/);

  assert.match(pageSource, /adviceFormDisabled\s*=\s*[\s\S]*advicePhase\s*===\s*["']loading["']/);
  assert.ok(
    (pageSource.match(/disabled=\{adviceFormDisabled\}/g) ?? []).length >= 5,
    "all four inputs and the submit action share the availability/loading gate",
  );
  assert.match(pageSource, /id=["']should-send-availability["']/);
  assert.match(pageSource, /aria-describedby=["']should-send-availability["']/);
  assert.match(pageSource, /adviceRequestGeneration\.current\s*\+=\s*1/);
  assert.match(
    pageSource,
    /requestGeneration\s*!==\s*adviceRequestGeneration\.current/,
    "an older response must not overwrite a newer draft or capability state",
  );
});

test("Should-Send copy is complete and exact in English and Chinese", () => {
  for (const [key, [en, zh]] of Object.entries(COPY)) {
    assert.equal(messages.en[key], en, `English ${key}`);
    assert.equal(messages.zh[key], zh, `Chinese ${key}`);
  }
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages.zh).sort());
});

test("Should-Send has static narrow, high-contrast, and reduced-motion contracts", () => {
  assert.match(cssSource, /\.should-send-(?:card|form|fields)/);
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*\.should-send-fields[\s\S]*grid-template-columns:\s*(?:minmax\(0,\s*1fr\)|1fr)/,
    "320px and 200% zoom collapse the fields to one column",
  );
  assert.match(cssSource, /@media\s*\(forced-colors:\s*active\)[\s\S]*\.should-send-/);
  assert.match(cssSource, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*\.should-send-/);
  assert.match(
    cssSource,
    /\.should-send-defer-timing\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/i,
    "Defer timing remains readable at 320px and 200% zoom",
  );
  assert.match(
    cssSource,
    /\.should-send-result\s+\.should-send-defer-timing\s+h5\s*\{[^}]*margin:\s*0\s+0\s+6px/i,
    "the nested timing heading must not inherit the later generic result spacing",
  );
  assert.match(
    cssSource,
    /@media\s*\(forced-colors:\s*active\)[\s\S]*\.should-send-defer-timing/,
  );
  assert.match(pageSource, /aria-invalid=/);
  assert.match(pageSource, /aria-describedby=/);
});
