import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import { messages } from "../.test-dist/i18n.js";


const pageSource = await readFile(
  new URL("../src/pages/AutomationPage.tsx", import.meta.url),
  "utf8",
);
const dataHealthPageSource = await readFile(
  new URL("../src/pages/DataHealthPage.tsx", import.meta.url),
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
const dataHealthPageFile = ts.createSourceFile(
  "DataHealthPage.tsx",
  dataHealthPageSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);
const serviceStatusLoad = await loadServiceStatusModule();

const COPY = {
  checkingServiceStatus: [
    "Checking service status…",
    "正在检查服务状态…",
  ],
  serviceStatusUnavailable: [
    "Service status is unavailable. Retry to check Observer and the optional Gateway.",
    "服务状态当前不可用。请重试以检查 Observer 和可选 Gateway。",
  ],
  serviceStatusLastKnown: [
    "Service status could not be refreshed. Showing the last known status.",
    "无法刷新服务状态，当前显示最近一次已知状态。",
  ],
  refreshServiceStatus: ["Refresh service status", "刷新服务状态"],
  retryServiceStatus: ["Retry service status", "重试服务状态"],
  serviceStatusUpdated: ["Service status updated.", "服务状态已更新。"],
  shouldSendAvailabilityUnavailable: [
    "Should-Send availability could not be verified. Retry service status.",
    "无法确认 Should-Send 是否可用。请重试服务状态。",
  ],
  gatewayUnavailableHint: [
    "Gateway is unavailable. Observer is unaffected.",
    "Gateway 当前不可用；Observer 不受影响。",
  ],
};


async function loadServiceStatusModule() {
  try {
    const source = await readFile(
      new URL("../src/serviceStatusViewModel.ts", import.meta.url),
      "utf8",
    );
    const output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "serviceStatusViewModel.ts",
    }).outputText;
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


function serviceStatusViewModel(input) {
  assert.equal(
    typeof serviceStatusLoad.module?.serviceStatusViewModel,
    "function",
    `src/serviceStatusViewModel.ts must export serviceStatusViewModel(input): ${serviceStatusLoad.error ?? "export missing"}`,
  );
  return serviceStatusLoad.module.serviceStatusViewModel(input);
}


function descendants(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}


function jsxElements(tagName, file = pageFile) {
  return descendants(
    file,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(file) === tagName,
  );
}


function openingAttribute(opening, name, file = pageFile) {
  return opening.attributes.properties.find(
    (attribute) =>
      ts.isJsxAttribute(attribute) && attribute.name.getText(file) === name,
  );
}


function literalAttribute(opening, name, file = pageFile) {
  const attribute = openingAttribute(opening, name, file);
  return attribute && ts.isJsxAttribute(attribute) && attribute.initializer &&
    ts.isStringLiteral(attribute.initializer)
    ? attribute.initializer.text
    : null;
}


function conditionalAncestorText(node, file = pageFile) {
  const guards = [];
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isConditionalExpression(current)) {
      guards.push(current.condition.getText(file));
    } else if (
      ts.isBinaryExpression(current) &&
      current.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
    ) {
      guards.push(current.left.getText(file));
    }
    if (ts.isFunctionLike(current)) break;
  }
  return guards.join(" ");
}


function importsModule(file, moduleName) {
  return descendants(
    file,
    (node) =>
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier) &&
      node.moduleSpecifier.text.replace(/\.js$/, "") === moduleName,
  ).length > 0;
}


function callsFunction(file, functionName) {
  return descendants(
    file,
    (node) =>
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === functionName,
  ).length > 0;
}


function dataHealthServiceSection(key) {
  const patterns = key === "observerTitle"
    ? [/t\.observerTitle\b/, /t\[[^\]]*\.observer\.titleKey\]/]
    : [/t\.gatewayTitle\b/, /t\[[^\]]*\.gateway\.titleKey\]/];
  return jsxElements("section", dataHealthPageFile).find((candidate) =>
    patterns.some((pattern) =>
      pattern.test(candidate.getText(dataHealthPageFile)),
    ),
  );
}


test("service status view model closes checking, failure, last-known, and ready states", () => {
  const cases = [
    {
      input: { checking: true, failed: false, hasSnapshot: false },
      expected: {
        state: "checking",
        messageKey: "checkingServiceStatus",
        actionKey: "refreshServiceStatus",
        actionDisabled: true,
        actionBusy: true,
        showSnapshot: false,
        announcementKey: "checkingServiceStatus",
      },
    },
    {
      input: { checking: false, failed: true, hasSnapshot: false },
      expected: {
        state: "initial_failure",
        messageKey: "serviceStatusUnavailable",
        actionKey: "retryServiceStatus",
        actionDisabled: false,
        actionBusy: false,
        showSnapshot: false,
        announcementKey: "serviceStatusUnavailable",
      },
    },
    {
      input: { checking: false, failed: true, hasSnapshot: true },
      expected: {
        state: "refresh_failure",
        messageKey: "serviceStatusLastKnown",
        actionKey: "retryServiceStatus",
        actionDisabled: false,
        actionBusy: false,
        showSnapshot: true,
        announcementKey: "serviceStatusLastKnown",
      },
    },
    {
      input: { checking: false, failed: false, hasSnapshot: true },
      expected: {
        state: "ready",
        messageKey: null,
        actionKey: "refreshServiceStatus",
        actionDisabled: false,
        actionBusy: false,
        showSnapshot: true,
        announcementKey: "serviceStatusUpdated",
      },
    },
  ];

  for (const { input, expected } of cases) {
    assert.deepEqual(serviceStatusViewModel(input), expected, expected.state);
  }
  assert.equal(
    serviceStatusViewModel({
      checking: true,
      failed: false,
      hasSnapshot: true,
    }).showSnapshot,
    true,
    "refreshing must retain an already validated snapshot",
  );

  const privateCanary = "RAW_TOKEN_PATH_ERROR_CANARY_b748";
  const closed = serviceStatusViewModel({
    checking: false,
    failed: true,
    hasSnapshot: false,
    error: privateCanary,
    token: privateCanary,
    path: privateCanary,
  });
  assert.doesNotMatch(JSON.stringify(closed), new RegExp(privateCanary));
});


test("service recovery and blocked Should-Send copy is exact in English and Chinese", () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages.zh).sort());
  for (const [key, [english, chinese]] of Object.entries(COPY)) {
    assert.equal(messages.en[key], english, `English ${key}`);
    assert.equal(messages.zh[key], chinese, `Chinese ${key}`);
  }
});


test("Automation uses one Observer-first read-only service recovery seam", () => {
  assert.match(pageSource, /from\s+["']\.\.\/serviceStatusViewModel["']/);
  assert.match(pageSource, /serviceStatusViewModel\s*\(/);
  assert.match(pageSource, /fetchRuntimeCapability\s*\(/);

  const serviceStatusIndex = pageSource.indexOf("service-status-strip");
  const observerIndex = pageSource.indexOf("t.observerTitle");
  assert.ok(serviceStatusIndex >= 0, "render the shared service-status strip");
  assert.ok(serviceStatusIndex < observerIndex, "service recovery precedes Observer");

  const serviceButtons = jsxElements("button").filter((button) =>
    button.openingElement.getText(pageFile).includes("service-status-action"),
  );
  assert.equal(serviceButtons.length, 1, "refresh and retry reuse one stable button");
  const serviceButtonText = serviceButtons[0].getText(pageFile);
  assert.match(serviceButtonText, /loadCapability/);
  assert.match(serviceButtonText, /serviceStatus\.actionKey/);
  assert.match(serviceButtonText, /serviceStatus\.actionDisabled/);
  assert.match(serviceButtonText, /serviceStatus\.actionBusy/);

  for (const key of ["observerTitle", "gatewayTitle"]) {
    const section = jsxElements("section").find((candidate) =>
      candidate.getText(pageFile).includes(`t.${key}`),
    );
    assert.ok(section, `render ${key}`);
    assert.match(
      conditionalAncestorText(section),
      /serviceStatus\.showSnapshot/,
      "an initial failure must not render an invented capability snapshot",
    );
  }

  assert.doesNotMatch(
    pageSource,
    /\b(?:gatewayToken|tokenPath|token_path|credential|authorization|rawError)\b|(?:error|err)\.(?:message|stack)/i,
  );
  assert.doesNotMatch(
    pageSource,
    /\b(?:enableGateway|disableGateway|clearGatewayCache|open_settings)\b|\bfetch\s*\(/,
    "service recovery remains a read-only repeat of the existing capability GET",
  );
});


test("Data Health reuses the service status view model and strip", () => {
  const strip = jsxElements("div", dataHealthPageFile).find(
    (candidate) =>
      literalAttribute(
        candidate.openingElement,
        "className",
        dataHealthPageFile,
      ) === "service-status-strip",
  );
  assert.deepEqual(
    {
      importsViewModel: importsModule(
        dataHealthPageFile,
        "../serviceStatusViewModel",
      ),
      buildsViewModel: callsFunction(
        dataHealthPageFile,
        "serviceStatusViewModel",
      ),
      rendersSharedStrip: Boolean(strip),
    },
    {
      importsViewModel: true,
      buildsViewModel: true,
      rendersSharedStrip: true,
    },
  );
});


test("Data Health capability facts follow last-good snapshot visibility", () => {
  const serviceSections = ["observerTitle", "gatewayTitle"].map((key) => {
    const section = dataHealthServiceSection(key);
    assert.ok(section, `render ${key}`);
    return section;
  });
  assert.deepEqual(
    serviceSections.map((section) =>
      /serviceStatus\.showSnapshot/.test(
        conditionalAncestorText(section, dataHealthPageFile),
      ),
    ),
    [true, true],
    "initial loading/failure hides facts while a last-good refresh retains them",
  );
});


test("Data Health keeps one stable Refresh or Retry action while busy", () => {
  const serviceButtons = jsxElements("button", dataHealthPageFile).filter(
    (button) =>
      button.openingElement.getText(dataHealthPageFile).includes(
        "service-status-action",
      ),
  );
  assert.equal(
    serviceButtons.length,
    1,
    "Refresh and Retry reuse one stable button node",
  );

  const serviceButton = serviceButtons[0];
  const serviceButtonText = serviceButton.getText(dataHealthPageFile);
  assert.match(serviceButtonText, /serviceStatus\.actionKey/);
  assert.match(serviceButtonText, /serviceStatus\.actionDisabled/);
  assert.match(serviceButtonText, /serviceStatus\.actionBusy/);
  assert.equal(
    conditionalAncestorText(serviceButton, dataHealthPageFile),
    "",
    "busy and retry states must not replace the focused action node",
  );
});


test("Data Health capability status owns one polite atomic live region", () => {
  const observerSources = descendants(
    dataHealthPageFile,
    (node) =>
      ts.isJsxExpression(node) &&
      node.expression &&
      /t\.observerSources\b/.test(node.expression.getText(dataHealthPageFile)),
  )[0];
  assert.ok(observerSources, "locate the boundary before Observer source status");

  const statusRegions = descendants(
    dataHealthPageFile,
    (node) => ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node),
  ).filter(
    (opening) =>
      opening.getStart(dataHealthPageFile) <
        observerSources.getStart(dataHealthPageFile) &&
      literalAttribute(opening, "role", dataHealthPageFile) === "status",
  );
  assert.equal(
    statusRegions.length,
    1,
    "Observer and Gateway capability state share one announcer",
  );
  assert.equal(
    literalAttribute(statusRegions[0], "aria-live", dataHealthPageFile),
    "polite",
  );
  assert.equal(
    literalAttribute(statusRegions[0], "aria-atomic", dataHealthPageFile),
    "true",
  );
  assert.match(
    statusRegions[0].parent.getText(dataHealthPageFile),
    /serviceStatus\.announcementKey/,
  );
});


test("Automation owns exactly one polite atomic live region", () => {
  const openings = descendants(
    pageFile,
    (node) => ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node),
  );
  const statusRegions = openings.filter(
    (opening) => literalAttribute(opening, "role") === "status",
  );
  assert.equal(statusRegions.length, 1, "the page has one shared status announcer");
  assert.equal(literalAttribute(statusRegions[0], "aria-live"), "polite");
  assert.equal(literalAttribute(statusRegions[0], "aria-atomic"), "true");
  assert.match(statusRegions[0].parent.getText(pageFile), /serviceStatus\.announcementKey/);
});


test("a refresh failure keeps the retained Gateway last-error fact", () => {
  const lastErrorRow = jsxElements("div").find(
    (candidate) =>
      literalAttribute(candidate.openingElement, "className") ===
        "automation-row" && candidate.getText(pageFile).includes("t.lastErrorLabel"),
  );
  assert.ok(lastErrorRow, "render the Gateway last-error fact row");

  const rowSource = lastErrorRow.getText(pageFile);
  assert.doesNotMatch(
    rowSource,
    /\bcapabilityError\b|\bgatewayErrorGeneric\b/,
    "a service refresh failure must not invent a Gateway failure",
  );
  assert.match(rowSource, /model\.gateway\.lastErrorMessageKey/);
  assert.match(rowSource, /["']—["']/);
});


test("service recovery has static narrow, forced-color, and reduced-motion contracts", () => {
  assert.match(
    cssSource,
    /\.service-status-strip\s*\{[^}]*flex-wrap:\s*wrap/i,
  );
  assert.match(
    cssSource,
    /\.service-status-(?:message|copy)\s*\{[^}]*overflow-wrap:\s*anywhere/i,
    "status copy wraps at 320px and 200% zoom",
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.service-status-action\s*\{[^}]*width:\s*100%/i,
    "the stable action becomes a full row at narrow effective widths",
  );
  assert.match(
    cssSource,
    /@media\s*\(forced-colors:\s*active\)[\s\S]*?\.service-status-/i,
  );
  assert.match(
    cssSource,
    /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.service-status-spinner(?:\.spinning)?\s*\{[^}]*animation:\s*none\s*!important/i,
    "the refresh spinner stops under reduced motion",
  );
});
