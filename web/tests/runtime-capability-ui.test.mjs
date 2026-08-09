import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import { messages } from "../.test-dist/i18n.js";
import { normalizeRuntimeCapability } from "../.test-dist/runtimeCapability.js";
import "./service-status-ui.test.mjs";


const apiSource = await readFile(
  new URL("../src/api.ts", import.meta.url),
  "utf8",
);
const automationSource = await readFile(
  new URL("../src/pages/AutomationPage.tsx", import.meta.url),
  "utf8",
);
const automationFile = ts.createSourceFile(
  "AutomationPage.tsx",
  automationSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

const viewModelLoad = await loadViewModelModule();
const apiLoad = await loadApiModule();

const FEATURE_IDS = [
  "listener",
  "should_send",
  "responses",
  "cache",
  "fallback",
  "pii_redaction",
  "streaming",
];
const MUTATING_ACTIONS = ["enable", "disable", "clear_cache"];
const ERROR_KEY_BY_CODE = {
  gateway_disabled: "gatewayErrorDisabled",
  gateway_starting: "gatewayErrorStarting",
  gateway_timeout: "gatewayErrorTimeout",
  gateway_unavailable: "gatewayErrorUnavailable",
  provider_unavailable: "gatewayErrorProviderUnavailable",
  credential_backend_unavailable:
    "gatewayErrorCredentialBackendUnavailable",
  capability_invalid: "gatewayErrorCapabilityInvalid",
};
const REQUIRED_COPY = {
  observerTitle: ["Observer", "Observer"],
  gatewayTitle: ["Gateway", "Gateway"],
  gatewayOptional: ["Optional", "可选"],
  operationalDisabled: ["Off", "已关闭"],
  operationalStarting: ["Starting…", "正在启动…"],
  operationalReady: ["Ready", "可用"],
  operationalDegraded: ["Limited", "部分可用"],
  operationalUnavailable: ["Unavailable", "不可用"],
  operationalUnknown: ["Status unknown", "状态未知"],
  modeObserve: ["Observe", "Observe"],
  modeAdvise: ["Advise", "Advise"],
  modeGateway: ["Gateway", "Gateway"],
  modeUnknown: ["Unknown", "未知"],
  valueYes: ["Yes", "是"],
  valueNo: ["No", "否"],
  valueUnknown: ["Unknown", "未知"],
  observeModeHint: [
    "Observer only; Gateway is off.",
    "仅使用 Observer；Gateway 已关闭。",
  ],
  adviseModeHint: [
    "Advice only; requests are not forwarded.",
    "仅提供建议；不会转发请求。",
  ],
  gatewayModeHint: [
    "Submitted requests may use Gateway.",
    "主动提交的请求可使用 Gateway。",
  ],
  gatewayDegradedHint: [
    "Gateway is limited. Observer data is still available.",
    "Gateway 部分可用；Observer 数据仍可使用。",
  ],
  gatewayUnavailableHint: [
    "Gateway is unavailable. Observer is unaffected.",
    "Gateway 当前不可用；Observer 不受影响。",
  ],
  gatewayUnknownHint: [
    "Gateway status could not be verified. Observer is unaffected.",
    "无法确认 Gateway 状态；Observer 不受影响。",
  ],
  retryGatewayStatus: ["Retry Gateway status", "重试 Gateway 状态"],
  gatewayErrorDisabled: ["Gateway is off.", "Gateway 已关闭。"],
  gatewayErrorStarting: [
    "Gateway is still starting.",
    "Gateway 仍在启动。",
  ],
  gatewayErrorTimeout: [
    "Gateway did not respond in time.",
    "Gateway 未及时响应。",
  ],
  gatewayErrorUnavailable: [
    "Gateway is unavailable.",
    "Gateway 当前不可用。",
  ],
  gatewayErrorProviderUnavailable: [
    "A configured Provider is unavailable.",
    "一个已配置的 Provider 不可用。",
  ],
  gatewayErrorCredentialBackendUnavailable: [
    "Secure credential storage is unavailable.",
    "安全凭证存储不可用。",
  ],
  gatewayErrorCapabilityInvalid: [
    "Gateway status could not be verified.",
    "无法确认 Gateway 状态。",
  ],
  gatewayErrorGeneric: [
    "Gateway needs attention.",
    "Gateway 需要处理。",
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


function callName(call, file) {
  if (ts.isIdentifier(call.expression)) return call.expression.text;
  if (ts.isPropertyAccessExpression(call.expression)) {
    return call.expression.name.text;
  }
  return call.expression.getText(file);
}


function jsxElements(tagName) {
  return descendants(
    automationFile,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(automationFile) === tagName,
  );
}


function conditionalAncestorText(node) {
  const guards = [];
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isConditionalExpression(current)) {
      guards.push(current.condition.getText(automationFile));
    } else if (
      ts.isBinaryExpression(current) &&
      [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(
        current.operatorToken.kind,
      )
    ) {
      guards.push(current.left.getText(automationFile));
    }
    if (ts.isFunctionLike(current)) break;
  }
  return guards.join(" ");
}


function caughtValueLeaks(file) {
  const leaks = [];
  const callbacks = descendants(
    file,
    (node) =>
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "catch",
  ).flatMap((call) =>
    call.arguments.filter(
      (argument) =>
        ts.isArrowFunction(argument) || ts.isFunctionExpression(argument),
    ),
  );

  for (const callback of callbacks) {
    const parameter = callback.parameters[0]?.name;
    if (!parameter || !ts.isIdentifier(parameter)) continue;
    const parameterName = parameter.text;
    for (const call of descendants(callback.body, ts.isCallExpression)) {
      const name = callName(call, file);
      if (!/^set[A-Z]/.test(name) && name !== "String" && name !== "stringify") {
        continue;
      }
      if (
        call.arguments.some((argument) =>
          descendants(
            argument,
            (node) => ts.isIdentifier(node) && node.text === parameterName,
          ).length > 0,
        )
      ) {
        leaks.push(call.getText(file));
      }
    }
  }

  for (const clause of descendants(file, ts.isCatchClause)) {
    const parameter = clause.variableDeclaration?.name;
    if (!parameter || !ts.isIdentifier(parameter)) continue;
    for (const call of descendants(clause.block, ts.isCallExpression)) {
      if (
        call.arguments.some((argument) =>
          descendants(
            argument,
            (node) => ts.isIdentifier(node) && node.text === parameter.text,
          ).length > 0,
        )
      ) {
        leaks.push(call.getText(file));
      }
    }
  }
  return leaks;
}


function runtimeCapabilityViewModel(snapshot) {
  assert.equal(
    typeof viewModelLoad.module?.runtimeCapabilityViewModel,
    "function",
    `src/runtimeCapabilityViewModel.ts must export runtimeCapabilityViewModel(snapshot): ${viewModelLoad.error ?? "export missing"}`,
  );
  return viewModelLoad.module.runtimeCapabilityViewModel(snapshot);
}


async function loadViewModelModule() {
  try {
    const source = await readFile(
      new URL("../src/runtimeCapabilityViewModel.ts", import.meta.url),
      "utf8",
    );
    let output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "runtimeCapabilityViewModel.ts",
    }).outputText;
    const runtimeCapabilityUrl = new URL(
      "../.test-dist/runtimeCapability.js",
      import.meta.url,
    ).href;
    const i18nUrl = new URL("../.test-dist/i18n.js", import.meta.url).href;
    output = output
      .replace(
        /from\s+["']\.\/runtimeCapability(?:\.js)?["']/g,
        `from "${runtimeCapabilityUrl}"`,
      )
      .replace(
        /from\s+["']\.\/i18n(?:\.js)?["']/g,
        `from "${i18nUrl}"`,
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


async function loadApiModule() {
  try {
    const shouldSendSource = await readFile(
      new URL("../src/shouldSendAdvice.ts", import.meta.url),
      "utf8",
    );
    const shouldSendOutput = ts.transpileModule(shouldSendSource, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "shouldSendAdvice.ts",
    }).outputText;
    const shouldSendUrl = `data:text/javascript;base64,${Buffer.from(shouldSendOutput).toString("base64")}`;
    let output = ts.transpileModule(apiSource, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "api.ts",
    }).outputText;
    const runtimeCapabilityUrl = new URL(
      "../.test-dist/runtimeCapability.js",
      import.meta.url,
    ).href;
    output = output
      .replace(
        /from\s+["']\.\/runtimeCapability(?:\.js)?["']/g,
        `from "${runtimeCapabilityUrl}"`,
      )
      .replace(
        /from\s+["']\.\/shouldSendAdvice(?:\.js)?["']/g,
        `from "${shouldSendUrl}"`,
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


function feature(overrides = {}) {
  return {
    support: "supported",
    enabled: true,
    configured: true,
    operational: "ready",
    ...overrides,
  };
}


function features(overrides = {}) {
  const result = Object.fromEntries(
    FEATURE_IDS.map((featureId) => [featureId, feature()]),
  );
  return { ...result, ...overrides };
}


function capability({ observer = {}, gateway = {} } = {}) {
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: {
      operational: "ready",
      generatedAt: "2026-08-08T15:00:00Z",
      lastGoodAt: "2026-08-08T14:59:00Z",
      dataRevision: 42,
      schemaVersion: "openusage/v1",
      ...observer,
    },
    gateway: {
      mode: "gateway",
      operational: "ready",
      features: features(),
      configuredProviderCount: 2,
      healthyProviderCount: 1,
      actions: [
        "enable",
        "disable",
        "open_settings",
        "retry",
        "learn_more",
        "clear_cache",
      ],
      lastError: null,
      ...gateway,
    },
  };
}


test("fetchRuntimeCapability uses one relative credential-free GET and normalizes unknown", async () => {
  assert.equal(
    typeof apiLoad.module?.fetchRuntimeCapability,
    "function",
    `api.ts must export fetchRuntimeCapability(): ${apiLoad.error ?? "export missing"}`,
  );
  const privateCanary = "PRIVATE_RUNTIME_DESCRIPTOR_CANARY_b76d";
  const wire = capability();
  wire.privateRuntimeDescriptor = privateCanary;
  wire.gateway.adapterCount = 5;
  wire.gateway.privateError = privateCanary;
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => wire,
    };
  };
  try {
    const result = await apiLoad.module.fetchRuntimeCapability();
    assert.deepEqual(
      result,
      normalizeRuntimeCapability(wire),
      "the public client must return the strict normalized model",
    );
    assert.doesNotMatch(JSON.stringify(result), new RegExp(privateCanary));
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1, "make exactly one renderer request");
  const [path, init = {}] = calls[0];
  assert.equal(path, "/gateway/v1/health");
  assert.equal(init.method ?? "GET", "GET");
  assert.equal(
    init.credentials,
    "omit",
    "browser fetch defaults to same-origin credentials, so this boundary must opt out explicitly",
  );
  for (const forbidden of ["headers", "body"]) {
    assert.ok(
      !(forbidden in init),
      `${forbidden} must not cross the credential-free renderer boundary`,
    );
  }
});


test("Automation removes private transport/commands and discards caught exceptions", () => {
  const forbidden = [
    /window\s*\.\s*location\s*\.\s*(?:hostname|host|origin|href)/i,
    /(?:127\.0\.0\.1|localhost|\[?::1\]?)/i,
    /unix[- ]socket|\.sock\b|~\/(?:\.local|Library)|named[- ]pipe/i,
    /\bbuildCurlCommand\b|\bcurlCommandLabel\b|\bbuildHelperCommand\b/i,
    /(?:^|[^\w])curl(?:[^\w]|$)|openusage-bar\s+helper/i,
    /navigator\s*\.\s*clipboard|\breadOnlyCommands\b|\bcommandHint\b|\bbundledHelper\b/i,
  ];
  for (const pattern of forbidden) {
    assert.doesNotMatch(automationSource, pattern, pattern.source);
  }
  assert.deepEqual(
    caughtValueLeaks(automationFile),
    [],
    "catch values must be discarded; store only a boolean or fixed localized state",
  );
  assert.doesNotMatch(
    automationSource,
    /\.(?:message|stack)\b|JSON\s*\.\s*stringify\s*\(\s*(?:e|err|error)\b/i,
    "raw exception text must never enter renderer state or markup",
  );
});


test("Automation renders independent Observer then Optional Gateway card shells", () => {
  const sections = jsxElements("section");
  const observerIndex = sections.findIndex((section) =>
    section.getText(automationFile).includes("t.observerTitle"),
  );
  const gatewayIndex = sections.findIndex((section) =>
    section.getText(automationFile).includes("t.gatewayTitle"),
  );
  assert.ok(observerIndex >= 0, "render an Observer card section");
  assert.ok(gatewayIndex >= 0, "render a Gateway card section");
  assert.ok(
    observerIndex < gatewayIndex,
    "Observer must appear before the optional Gateway",
  );
  assert.match(
    sections[gatewayIndex].getText(automationFile),
    /t\.gatewayOptional/,
    "Gateway must be visibly labeled Optional",
  );
  assert.doesNotMatch(
    conditionalAncestorText(sections[observerIndex]),
    /gateway/i,
    "Gateway fetch/state must never control whether the Observer shell renders",
  );
  assert.match(
    automationSource,
    /runtimeCapabilityViewModel/,
    "Automation must consume the pure capability presentation seam",
  );

  const unknownFeatures = Object.fromEntries(
    FEATURE_IDS.map((featureId) => [
      featureId,
      feature({
        support: "unknown",
        enabled: "unknown",
        configured: "unknown",
        operational: "unknown",
      }),
    ]),
  );
  const model = runtimeCapabilityViewModel(
    capability({
      gateway: {
        mode: "unknown",
        operational: "unknown",
        features: unknownFeatures,
        configuredProviderCount: null,
        healthyProviderCount: null,
        lastError: { code: "capability_invalid", retryable: false },
      },
    }),
  );
  assert.equal(model.observer.titleKey, "observerTitle");
  assert.equal(model.observer.operationalKey, "operationalReady");
  assert.equal(model.observer.tone, "positive");
  assert.equal(
    model.observer.lastGoodAtText,
    "2026-08-08T14:59:00Z",
    "unknown Gateway facts must not erase the Observer last-good fact",
  );
  assert.equal(model.gateway.titleKey, "gatewayTitle");
  assert.equal(model.gateway.optionalKey, "gatewayOptional");
  assert.equal(model.gateway.modeKey, "modeUnknown");
  assert.equal(model.gateway.overallOperationalKey, "operationalUnknown");
  assert.equal(model.gateway.tone, "neutral");
  assert.equal(model.gateway.configuredProviderCountText, "—");
  assert.equal(model.gateway.healthyProviderCountText, "—");
  assert.equal(model.gateway.consequenceKey, "gatewayUnknownHint");
});


test("Gateway presentation keeps explicit facts separate and never infers readiness", () => {
  const snapshot = capability({
    gateway: {
      mode: "gateway",
      operational: "degraded",
      features: features({
        listener: feature({ operational: "starting" }),
        cache: feature({ enabled: false, operational: "disabled" }),
      }),
      configuredProviderCount: 2,
      healthyProviderCount: 1,
      lastError: {
        code: "provider_unavailable",
        retryable: true,
        observedAt: "2026-08-08T14:58:00Z",
      },
    },
  });
  const model = runtimeCapabilityViewModel(snapshot);
  assert.equal(model.gateway.modeKey, "modeGateway");
  assert.equal(model.gateway.overallOperationalKey, "operationalDegraded");
  assert.equal(model.gateway.listenerOperationalKey, "operationalStarting");
  assert.equal(model.gateway.configuredProviderCountText, "2");
  assert.equal(model.gateway.healthyProviderCountText, "1");
  assert.equal(model.gateway.cacheEnabledKey, "valueNo");
  assert.equal(
    model.gateway.lastErrorMessageKey,
    "gatewayErrorProviderUnavailable",
  );
  assert.equal(model.gateway.consequenceKey, "gatewayDegradedHint");

  const inferenceTrap = capability({
    gateway: {
      mode: "gateway",
      operational: "ready",
      features: features({
        listener: feature({
          support: "supported",
          enabled: true,
          configured: true,
          operational: "unknown",
        }),
        cache: feature({
          enabled: "unknown",
          configured: "unknown",
          operational: "unknown",
        }),
      }),
      configuredProviderCount: null,
      healthyProviderCount: null,
      adapterCount: 5,
    },
  });
  const unknown = runtimeCapabilityViewModel(inferenceTrap);
  assert.equal(unknown.gateway.listenerOperationalKey, "operationalUnknown");
  assert.equal(unknown.gateway.configuredProviderCountText, "—");
  assert.equal(unknown.gateway.healthyProviderCountText, "—");
  assert.equal(unknown.gateway.cacheEnabledKey, "valueUnknown");
  assert.equal(unknown.gateway.listenerTone, "neutral");
  assert.doesNotMatch(JSON.stringify(unknown), /adapterCount|5 configured|5 healthy/i);
});


test("mode consequences remain explicit for Observe, Advise, and Gateway", () => {
  const cases = [
    {
      mode: "observe",
      operational: "disabled",
      listener: feature({
        enabled: false,
        configured: false,
        operational: "disabled",
      }),
      expected: "observeModeHint",
    },
    {
      mode: "advise",
      operational: "ready",
      listener: feature(),
      expected: "adviseModeHint",
    },
    {
      mode: "gateway",
      operational: "ready",
      listener: feature(),
      expected: "gatewayModeHint",
    },
  ];
  for (const item of cases) {
    const model = runtimeCapabilityViewModel(
      capability({
        gateway: {
          mode: item.mode,
          operational: item.operational,
          features: features({ listener: item.listener }),
        },
      }),
    );
    assert.equal(model.gateway.consequenceKey, item.expected, item.mode);
  }
});


test("lastError uses only allowlisted localized message keys", () => {
  for (const [code, expected] of Object.entries(ERROR_KEY_BY_CODE)) {
    const model = runtimeCapabilityViewModel(
      capability({ gateway: { lastError: { code, retryable: true } } }),
    );
    assert.equal(model.gateway.lastErrorMessageKey, expected, code);
  }

  const privateCanary = "RAW_PROVIDER_EXCEPTION_CANARY_4f1a";
  const unknown = runtimeCapabilityViewModel(
    capability({
      gateway: {
        lastError: {
          code: privateCanary,
          retryable: false,
          message: privateCanary,
          stack: privateCanary,
        },
      },
    }),
  );
  assert.equal(unknown.gateway.lastErrorMessageKey, "gatewayErrorGeneric");
  assert.doesNotMatch(JSON.stringify(unknown), new RegExp(privateCanary));

  const getterCanary = "THROWING_ERROR_CODE_GETTER_CANARY_9c31";
  const hostile = capability({
    gateway: {
      lastError: { code: "provider_unavailable", retryable: true },
    },
  });
  Object.defineProperty(hostile.gateway.lastError, "code", {
    enumerable: true,
    get() {
      throw new Error(getterCanary);
    },
  });
  const getterSafe = runtimeCapabilityViewModel(hostile);
  assert.equal(
    getterSafe.gateway.lastErrorMessageKey,
    "gatewayErrorCapabilityInvalid",
  );
  assert.doesNotMatch(JSON.stringify(getterSafe), new RegExp(getterCanary));
});


test("Automation exposes only safe Retry while host mutations are unavailable", () => {
  const model = runtimeCapabilityViewModel(capability());
  assert.ok(model.gateway.actions.includes("retry"), "safe Retry remains available");
  for (const action of MUTATING_ACTIONS) {
    assert.ok(
      !model.gateway.actions.includes(action),
      `${action} must stay hidden without a trusted host executor`,
    );
  }
  assert.match(
    automationSource,
    /t\.retryGatewayStatus/,
    "Automation must render the localized Gateway Retry control",
  );
  assert.match(
    automationSource,
    /fetchRuntimeCapability/,
    "Retry must re-run the fixed safe capability GET",
  );
  assert.doesNotMatch(
    automationSource,
    /t\.(?:enableGateway|disableGateway|clearGatewayCache)|["'](?:enable|disable|clear_cache)["']/,
    "no host mutation is rendered or dispatched in this phase",
  );
  assert.doesNotMatch(
    automationSource,
    /\bfetch\s*\(/,
    "Automation must not bypass the allowlisted API helper",
  );
});


test("Gateway capability copy has exact English and Chinese key parity", () => {
  assert.deepEqual(
    Object.keys(messages.zh).sort(),
    Object.keys(messages.en).sort(),
    "every renderer message key must exist in both languages",
  );
  for (const [key, [english, chinese]] of Object.entries(REQUIRED_COPY)) {
    assert.equal(messages.en[key], english, `English ${key}`);
    assert.equal(messages.zh[key], chinese, `Chinese ${key}`);
  }
});
