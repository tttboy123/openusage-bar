import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import { messages } from "../.test-dist/i18n.js";

const pageSource = await readFile(
  new URL("../src/pages/DataHealthPage.tsx", import.meta.url),
  "utf8",
);
const pageFile = ts.createSourceFile(
  "DataHealthPage.tsx",
  pageSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);
const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const apiLoad = await loadTypeScriptModule(apiSource, "api.ts");
const dataHealthViewModelLoad = await loadDataHealthViewModel();

const FEATURE_IDS = [
  "listener",
  "should_send",
  "responses",
  "cache",
  "fallback",
  "pii_redaction",
  "streaming",
];

const REQUIRED_COPY = {
  setupNeeded: ["Setup needed", "需要配置"],
  notActiveInMode: ["Not active in this mode", "当前模式未启用"],
  observerSources: ["Observer sources", "Observer 数据源"],
  gatewayProviders: ["Gateway Providers", "Gateway Provider"],
  providerHealthUnknown: [
    "Provider health is not reported.",
    "未报告 Provider 健康状态。",
  ],
  noGatewayProviders: [
    "No Gateway Providers configured.",
    "尚未配置 Gateway Provider。",
  ],
  providerCount: [
    "{healthy} of {configured} Providers healthy",
    "{configured} 个 Provider 中 {healthy} 个健康",
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

function jsxPresentationIndex(patterns) {
  const positions = descendants(
    pageFile,
    (node) =>
      ts.isJsxExpression(node) &&
      node.expression &&
      patterns.some((pattern) => pattern.test(node.expression.getText(pageFile))),
  ).map((node) => node.getStart(pageFile));
  return positions.length > 0 ? Math.min(...positions) : -1;
}

function namedFunction(name) {
  return descendants(pageFile, (node) => {
    if (ts.isFunctionDeclaration(node)) return node.name?.text === name;
    return (
      (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) &&
      ts.isVariableDeclaration(node.parent) &&
      ts.isIdentifier(node.parent.name) &&
      node.parent.name.text === name
    );
  })[0];
}

function gatewayProviderRegion(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (
      ts.isJsxElement(current) &&
      /providerHealth\.(?:summaryKey|configuredCountText|healthyCountText)/.test(
        current.getText(pageFile),
      )
    ) {
      return current;
    }
  }
  return undefined;
}

function capabilityState() {
  const declaration = descendants(
    pageFile,
    (node) =>
      ts.isVariableDeclaration(node) &&
      ts.isArrayBindingPattern(node.name) &&
      ts.isCallExpression(node.initializer) &&
      node.initializer.expression.getText(pageFile) === "useState" &&
      node.name.elements.some(
        (element) =>
          ts.isBindingElement(element) &&
          ts.isIdentifier(element.name) &&
          /capability/i.test(element.name.text) &&
          !/error/i.test(element.name.text),
      ),
  )[0];
  if (!declaration || !ts.isArrayBindingPattern(declaration.name)) return null;
  const names = declaration.name.elements.map((element) =>
    ts.isBindingElement(element) && ts.isIdentifier(element.name)
      ? element.name.text
      : "",
  );
  return { stateName: names[0], setterName: names[1] };
}

function retainsLastGoodCapability(state) {
  if (!state?.setterName) return false;
  const calls = descendants(
    pageFile,
    (node) =>
      ts.isCallExpression(node) &&
      node.expression.getText(pageFile) === state.setterName,
  );
  return calls.some((call) => {
    const argument = call.arguments[0];
    if (!argument) return false;
    if (
      descendants(
        argument,
        (node) =>
          ts.isBinaryExpression(node) &&
          node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken,
      ).length > 0
    ) {
      return true;
    }
    const valueName = ts.isIdentifier(argument) ? argument.text : "";
    if (!valueName) return false;
    for (let current = call.parent; current; current = current.parent) {
      if (
        ts.isIfStatement(current) &&
        new RegExp(`\\b${valueName}\\b`).test(current.expression.getText(pageFile))
      ) {
        return true;
      }
      if (ts.isFunctionLike(current)) break;
    }
    return false;
  });
}

function capability(overrides = {}) {
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: {
      operational: "ready",
      generatedAt: "2026-08-08T15:00:00Z",
      lastGoodAt: "2026-08-08T14:59:00Z",
      dataRevision: 42,
      schemaVersion: "openusage/v1",
    },
    gateway: {
      mode: "gateway",
      operational: "ready",
      features: Object.fromEntries(FEATURE_IDS.map((id) => [id, feature()])),
      configuredProviderCount: 2,
      healthyProviderCount: 2,
      actions: ["retry"],
      lastError: null,
      ...overrides,
    },
  };
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

function inactiveFeatures() {
  return Object.fromEntries(
    FEATURE_IDS.map((id) => [
      id,
      feature({ enabled: false, configured: false, operational: "disabled" }),
    ]),
  );
}

function dataHealthCapabilityViewModel(snapshot) {
  assert.equal(
    typeof dataHealthViewModelLoad.module?.dataHealthCapabilityViewModel,
    "function",
    `src/dataHealthCapabilityViewModel.ts must export dataHealthCapabilityViewModel(snapshot): ${dataHealthViewModelLoad.error ?? "export missing"}`,
  );
  return dataHealthViewModelLoad.module.dataHealthCapabilityViewModel(snapshot);
}

async function loadDataHealthViewModel() {
  try {
    const source = await readFile(
      new URL("../src/dataHealthCapabilityViewModel.ts", import.meta.url),
      "utf8",
    );
    return await loadTypeScriptModule(source, "dataHealthCapabilityViewModel.ts");
  } catch (error) {
    return {
      module: null,
      error: error instanceof Error ? error.message : "could not load module",
    };
  }
}

async function loadTypeScriptModule(source, fileName) {
  try {
    let output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName,
    }).outputText;
    const compiled = {
      runtimeCapability: new URL(
        "../.test-dist/runtimeCapability.js",
        import.meta.url,
      ).href,
      runtimeCapabilityViewModel: new URL(
        "../.test-dist/runtimeCapabilityViewModel.js",
        import.meta.url,
      ).href,
      i18n: new URL("../.test-dist/i18n.js", import.meta.url).href,
    };
    for (const [moduleName, moduleUrl] of Object.entries(compiled)) {
      output = output.replace(
        new RegExp(`from\\s+["']\\./${moduleName}(?:\\.js)?["']`, "g"),
        `from "${moduleUrl}"`,
      );
    }
    if (/from\s+["']\.\/shouldSendAdvice(?:\.js)?["']/.test(output)) {
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
      output = output.replace(
        /from\s+["']\.\/shouldSendAdvice(?:\.js)?["']/g,
        `from "${shouldSendUrl}"`,
      );
    }
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

test("Data Health orders independent Observer and Optional Gateway services before Observer sources", () => {
  const observerIndex = jsxPresentationIndex([
    /t\.observerTitle\b/,
    /t\[[^\]]*\.observer\.titleKey\]/,
  ]);
  const gatewayIndex = jsxPresentationIndex([
    /t\.gatewayTitle\b/,
    /t\[[^\]]*\.gateway\.titleKey\]/,
  ]);
  const optionalIndex = jsxPresentationIndex([
    /t\.gatewayOptional\b/,
    /t\[[^\]]*\.gateway\.optionalKey\]/,
  ]);
  const sourcesIndex = jsxPresentationIndex([/t\.observerSources\b/]);

  assert.ok(observerIndex >= 0, "render the Observer service first");
  assert.ok(gatewayIndex > observerIndex, "render Gateway after Observer");
  assert.ok(
    optionalIndex >= gatewayIndex && optionalIndex < sourcesIndex,
    "label Gateway Optional inside the service hierarchy",
  );
  assert.ok(
    sourcesIndex > gatewayIndex,
    "label Observer sources after both service cards",
  );

  const issueCount = descendants(
    pageFile,
    (node) =>
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "issueCount",
  )[0];
  assert.ok(issueCount?.initializer, "retain the Observer source issue count");
  assert.match(issueCount.initializer.getText(pageFile), /rows\s*\.\s*filter/);
  assert.doesNotMatch(
    issueCount.initializer.getText(pageFile),
    /gateway|capability|providerHealth/i,
    "Gateway unknown/not-reported state must not increment Observer source issues",
  );
});

test("Data Health uses the fixed capability helper and retains last-good facts on null or refresh error", async () => {
  assert.equal(
    typeof apiLoad.module?.fetchRuntimeCapability,
    "function",
    `api.ts must export fetchRuntimeCapability(): ${apiLoad.error ?? "export missing"}`,
  );
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true, status: 200, json: async () => capability() };
  };
  try {
    await apiLoad.module.fetchRuntimeCapability();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/gateway/v1/health");
  assert.equal(calls[0][1]?.method ?? "GET", "GET");
  assert.equal(calls[0][1]?.credentials, "omit");

  const violations = [];
  if (!/\bfetchRuntimeCapability\b/.test(pageSource)) {
    violations.push("DataHealthPage does not call the API helper");
  }
  if (/\bfetch\s*\(/.test(pageSource)) {
    violations.push("DataHealthPage bypasses the API helper");
  }
  const caughtValues = descendants(pageFile, (node) => ts.isCatchClause(node))
    .map((clause) => clause.variableDeclaration?.name.getText(pageFile))
    .filter(Boolean);
  if (caughtValues.length > 0) {
    violations.push(`caught exceptions are retained: ${caughtValues.join(", ")}`);
  }
  if (/\.(?:message|stack)\b|JSON\s*\.\s*stringify\s*\(\s*(?:e|err|error)\b/i.test(pageSource)) {
    violations.push("raw exception details can enter renderer state");
  }

  const state = capabilityState();
  if (!state) {
    violations.push("no last-good capability snapshot state exists");
  } else {
    const clearsSnapshot = descendants(
      pageFile,
      (node) =>
        ts.isCallExpression(node) &&
        node.expression.getText(pageFile) === state.setterName &&
        node.arguments.some((argument) => argument.kind === ts.SyntaxKind.NullKeyword),
    );
    if (clearsSnapshot.length > 0) {
      violations.push("refresh clears the last-good capability snapshot");
    }
    if (!retainsLastGoodCapability(state)) {
      violations.push("null capability results do not retain the last-good snapshot");
    }
  }

  const booleanErrorState = descendants(
    pageFile,
    (node) =>
      ts.isVariableDeclaration(node) &&
      ts.isArrayBindingPattern(node.name) &&
      node.name.getText(pageFile).match(/capability.*error|error.*capability/i) &&
      ts.isCallExpression(node.initializer) &&
      node.initializer.arguments[0]?.kind === ts.SyntaxKind.FalseKeyword,
  )[0];
  if (!booleanErrorState) {
    violations.push("capability request failure is not stored as a boolean");
  } else if (ts.isArrayBindingPattern(booleanErrorState.name)) {
    const setter = booleanErrorState.name.elements[1];
    const setterName =
      setter && ts.isBindingElement(setter) && ts.isIdentifier(setter.name)
        ? setter.name.text
        : "";
    const assignments = descendants(
      pageFile,
      (node) =>
        ts.isCallExpression(node) &&
        node.expression.getText(pageFile) === setterName,
    );
    if (
      assignments.length === 0 ||
      assignments.some(
        (call) =>
          !call.arguments[0] ||
          ![ts.SyntaxKind.TrueKeyword, ts.SyntaxKind.FalseKeyword].includes(
            call.arguments[0].kind,
          ),
      )
    ) {
      violations.push("capability request failure stores non-boolean data");
    }
  }
  assert.deepEqual(violations, []);
});

test("the pure Data Health presentation seam preserves layers and explicit Provider count semantics", () => {
  const privateCanary = "PRIVATE_ADAPTER_CATALOG_CANARY_7df2";
  const unknown = dataHealthCapabilityViewModel({
    ...capability(),
    gateway: { adapterCount: 5, privateError: privateCanary },
  });
  assert.equal(unknown.observer.operationalKey, "operationalReady");
  assert.equal(unknown.observer.tone, "positive");
  assert.equal(unknown.gateway.overallOperationalKey, "operationalUnknown");
  assert.equal(unknown.gateway.tone, "neutral");
  assert.equal(unknown.providerHealth.titleKey, "gatewayProviders");
  assert.equal(unknown.providerHealth.summaryKey, "providerHealthUnknown");
  assert.equal(unknown.providerHealth.configuredCountText, "—");
  assert.equal(unknown.providerHealth.healthyCountText, "—");
  assert.equal(unknown.providerHealth.tone, "neutral");
  assert.equal(unknown.providerHealth.isIssue, false);
  assert.doesNotMatch(JSON.stringify(unknown), /adapterCount|PRIVATE_ADAPTER/);

  for (const mode of ["observe", "advise"]) {
    const model = dataHealthCapabilityViewModel(
      capability({
        mode,
        operational: mode === "observe" ? "disabled" : "ready",
        features: mode === "observe" ? inactiveFeatures() : capability().gateway.features,
        configuredProviderCount: mode === "observe" ? null : 2,
        healthyProviderCount: mode === "observe" ? null : 2,
      }),
    );
    assert.equal(model.providerHealth.summaryKey, "notActiveInMode", mode);
    assert.equal(model.providerHealth.tone, "neutral", mode);
    assert.equal(model.providerHealth.isIssue, false, mode);
  }

  const cases = [
    {
      configured: null,
      healthy: null,
      summaryKey: "providerHealthUnknown",
      configuredText: "—",
      healthyText: "—",
    },
    {
      configured: 0,
      healthy: 0,
      summaryKey: "noGatewayProviders",
      configuredText: "0",
      healthyText: "0",
    },
    {
      configured: 3,
      healthy: null,
      summaryKey: "providerHealthUnknown",
      configuredText: "3",
      healthyText: "—",
    },
    {
      configured: 3,
      healthy: 2,
      summaryKey: "providerCount",
      configuredText: "3",
      healthyText: "2",
    },
  ];
  for (const item of cases) {
    const model = dataHealthCapabilityViewModel(
      capability({
        configuredProviderCount: item.configured,
        healthyProviderCount: item.healthy,
        adapterCount: 5,
      }),
    );
    assert.equal(model.providerHealth.summaryKey, item.summaryKey);
    assert.equal(
      model.providerHealth.configuredCountText,
      item.configuredText,
    );
    assert.equal(model.providerHealth.healthyCountText, item.healthyText);
  }
});

test("Data Health capability copy has exact English and Chinese parity", () => {
  assert.deepEqual(
    Object.keys(messages.zh).sort(),
    Object.keys(messages.en).sort(),
    "every renderer message key must exist in both languages",
  );
  const actual = Object.fromEntries(
    Object.keys(REQUIRED_COPY).map((key) => [
      key,
      [messages.en[key], messages.zh[key]],
    ]),
  );
  assert.deepEqual(actual, REQUIRED_COPY);
});

test("Data Health exposes one read-only service refresh and keeps Gateway Provider health independent of catalogs", () => {
  const serviceActions = descendants(
    pageFile,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(pageFile) === "button" &&
      /service-status-action/.test(node.openingElement.getText(pageFile)),
  );
  assert.equal(
    serviceActions.length,
    1,
    "render one stable Refresh/Retry service action",
  );
  const serviceAction = serviceActions[0];
  assert.match(serviceAction.getText(pageFile), /serviceStatus\.actionKey/);
  assert.doesNotMatch(
    pageSource,
    /t\.(?:enableGateway|disableGateway|clearGatewayCache)|["'](?:enable|disable|clear_cache)["']/,
    "Data Health must not expose mutation actions without a trusted host executor",
  );
  assert.doesNotMatch(
    pageSource,
    /bearer|authorization|apiKey|secret|privateError|rawError|\.message\b|\.stack\b/i,
    "private transport and raw errors must not enter Data Health markup",
  );

  const providerTitle = descendants(
    pageFile,
    (node) =>
      ts.isJsxExpression(node) &&
      node.expression &&
      (/t\.gatewayProviders\b/.test(node.expression.getText(pageFile)) ||
        /providerHealth\.titleKey/.test(node.expression.getText(pageFile))),
  )[0];
  const providerSection = providerTitle && gatewayProviderRegion(providerTitle);
  assert.ok(providerSection, "render a separately labeled Gateway Provider region");
  assert.doesNotMatch(
    providerSection.getText(pageFile),
    /providerByFamily|quickByFamily|fetchProviders|adapter(?:Count|Catalog)|\bproviders\s*\.\s*(?:map|length)/i,
    "Gateway Provider health must come only from the normalized capability seam",
  );
  assert.match(
    providerSection.getText(pageFile),
    /providerHealth/,
    "Gateway Provider health must consume the pure presentation result",
  );

  const onClick = serviceAction.openingElement.attributes.properties.find(
    (property) => ts.isJsxAttribute(property) && property.name.text === "onClick",
  );
  const expression =
    onClick?.initializer && ts.isJsxExpression(onClick.initializer)
      ? onClick.initializer.expression
      : undefined;
  assert.ok(expression, "the service action invokes a safe refresh");
  const handlerNames = new Set();
  if (ts.isIdentifier(expression)) handlerNames.add(expression.text);
  for (const call of descendants(
    expression,
    (node) => ts.isCallExpression(node) && ts.isIdentifier(node.expression),
  )) {
    handlerNames.add(call.expression.text);
  }
  const handlers = [...handlerNames]
    .map((name) => namedFunction(name))
    .filter(Boolean);
  assert.ok(handlers.length > 0, "the service action resolves to a local refresh");
  const handlerText = handlers.map((handler) => handler.getText(pageFile)).join(" ");
  const calledText = handlers
    .flatMap((handler) =>
      descendants(
        handler,
        (node) => ts.isCallExpression(node) && ts.isIdentifier(node.expression),
      ),
    )
    .map((call) => namedFunction(call.expression.text)?.getText(pageFile) ?? "")
    .join(" ");
  assert.match(
    `${handlerText} ${calledText}`,
    /fetchRuntimeCapability/,
    "Refresh and Retry must re-run the fixed capability helper",
  );
});
