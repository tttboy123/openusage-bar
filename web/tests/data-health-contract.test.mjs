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
const viewStateSource = await readFile(
  new URL("../src/dataHealthViewState.ts", import.meta.url),
  "utf8",
);
const viewStateOutput = ts.transpileModule(viewStateSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: "dataHealthViewState.ts",
}).outputText;
const viewState = await import(
  `data:text/javascript;base64,${Buffer.from(viewStateOutput).toString("base64")}`
);


function descendants(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}


function jsxAttribute(opening, name) {
  return opening.attributes.properties.find(
    (property) =>
      ts.isJsxAttribute(property) && property.name.text === name,
  );
}


function jsxAttributeExpression(opening, name) {
  const attribute = jsxAttribute(opening, name);
  if (
    !attribute ||
    !attribute.initializer ||
    !ts.isJsxExpression(attribute.initializer)
  ) {
    return undefined;
  }
  return attribute.initializer.expression;
}


function jsxElements(tagName) {
  return descendants(
    pageFile,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(pageFile) === tagName,
  );
}


function jsxElementWithText(tagName, text) {
  return jsxElements(tagName).find((element) =>
    element.getText(pageFile).includes(text),
  );
}


function nearestNamedFunction(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isFunctionDeclaration(current) && current.name) return current;
    if (
      (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) &&
      ts.isVariableDeclaration(current.parent) &&
      ts.isIdentifier(current.parent.name)
    ) {
      return current;
    }
  }
  return undefined;
}


function functionName(fn) {
  if (ts.isFunctionDeclaration(fn)) return fn.name?.text;
  if (
    (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) &&
    ts.isVariableDeclaration(fn.parent) &&
    ts.isIdentifier(fn.parent.name)
  ) {
    return fn.parent.name.text;
  }
  return undefined;
}


function declarationForIdentifier(name, scope) {
  return descendants(
    scope,
    (node) =>
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === name &&
      Boolean(node.initializer),
  )[0];
}


function resolvedInitializerText(name, scope, seen = new Set()) {
  if (seen.has(name)) return "";
  seen.add(name);
  const declaration = declarationForIdentifier(name, scope);
  if (!declaration?.initializer) return "";

  let resolved = declaration.initializer.getText(pageFile);
  for (const identifier of descendants(
    declaration.initializer,
    ts.isIdentifier,
  )) {
    resolved += ` ${resolvedInitializerText(identifier.text, scope, seen)}`;
  }
  return resolved;
}


function guardForElement(element) {
  for (let current = element.parent; current; current = current.parent) {
    if (ts.isConditionalExpression(current)) return current.condition;
    if (
      ts.isBinaryExpression(current) &&
      [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(
        current.operatorToken.kind,
      )
    ) {
      return current.left;
    }
    if (ts.isFunctionLike(current)) return undefined;
  }
  return undefined;
}


function resolvedExpressionText(expression, scope) {
  let resolved = expression.getText(pageFile);
  for (const identifier of descendants(expression, ts.isIdentifier)) {
    resolved += ` ${resolvedInitializerText(identifier.text, scope)}`;
  }
  return resolved;
}


test("an initial load failure cannot present zero sources as healthy", () => {
  const summary = jsxElements("p").find(
    (element) =>
      jsxAttribute(element.openingElement, "className")?.initializer?.text ===
      "health-summary",
  );
  assert.ok(summary, "Data Health must retain its status summary");

  const guard = guardForElement(summary);
  assert.ok(
    guard,
    "the health summary must be conditional so an initial failed load cannot announce 0 healthy sources",
  );

  const pageFunction = nearestNamedFunction(summary);
  assert.ok(pageFunction, "the summary must belong to a named component");
  const resolvedGuard = resolvedExpressionText(guard, pageFunction);
  assert.match(
    resolvedGuard,
    /\berror\b/,
    "the summary guard must account for request failure",
  );
  assert.match(
    resolvedGuard,
    /sources\s*\.\s*length/,
    "the summary guard must distinguish a last-good source snapshot from an empty initial state",
  );
});


test("request failures use generic localized copy and remain retryable", () => {
  assert.doesNotMatch(
    pageSource,
    /\b(?:e|error)\s*instanceof\s+Error\s*\?\s*(?:e|error)\.message/,
    "exception messages must not be copied into render state",
  );
  assert.doesNotMatch(
    pageSource,
    /\{\s*error\s*\}/,
    "request error state must not be rendered verbatim",
  );
  assert.match(
    pageSource,
    /t\.healthLoadError/,
    "request failure must render the localized generic healthLoadError message",
  );

  const errorCopy = jsxElements("p").find((element) =>
    element.getText(pageFile).includes("t.healthLoadError"),
  );
  assert.ok(errorCopy, "generic request failure copy must be visible");
  const retryButton = descendants(
    errorCopy,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(pageFile) === "button" &&
      node.getText(pageFile).includes("t.retry"),
  )[0];
  assert.ok(retryButton, "generic request failure must offer Retry");
});


test("ok and available share a healthy cause while unknown errors stay generic", () => {
  assert.equal(
    typeof viewState.dataHealthCauseKey,
    "function",
    "extract source cause classification into the pure dataHealthCauseKey helper",
  );
  assert.equal(viewState.dataHealthCauseKey({ state: "ok" }), "healthOk");
  assert.equal(
    viewState.dataHealthCauseKey({ state: "AVAILABLE" }),
    "healthOk",
    "available is a healthy source state, just like ok",
  );

  for (const item of [
    { state: "future_state", errorCode: "future_error" },
    { state: "<img src=x onerror=alert(1)>" },
    { errorCode: "private-upstream-detail" },
    {},
  ]) {
    assert.equal(
      viewState.dataHealthCauseKey(item),
      "healthUnknown",
      JSON.stringify(item),
    );
  }
  assert.doesNotMatch(messages.en.healthUnknown, /\{code\}/);
  assert.doesNotMatch(messages.zh.healthUnknown, /\{code\}/);
});


test("source-state presentation is allowlisted and returns localized message keys", () => {
  assert.equal(
    typeof viewState.dataHealthStatePresentation,
    "function",
    "extract pill classification into the pure dataHealthStatePresentation helper",
  );

  const ok = viewState.dataHealthStatePresentation({ state: "ok" });
  const available = viewState.dataHealthStatePresentation({ state: "available" });
  assert.deepEqual(available, ok, "available and ok need the same presentation");
  assert.equal(ok.kind, "ok");

  assert.equal(
    viewState.dataHealthStatePresentation({ state: "stale" }).kind,
    "warn",
  );
  assert.equal(
    viewState.dataHealthStatePresentation({ state: "temporarily_unavailable" }).kind,
    "warn",
  );
  assert.equal(
    viewState.dataHealthStatePresentation({ state: "error" }).kind,
    "bad",
  );

  const generic = viewState.dataHealthStatePresentation({});
  for (const rawState of [
    "future_state_FROM_PROVIDER",
    "<script>alert(1)</script>",
    "available_later",
  ]) {
    const presentation = viewState.dataHealthStatePresentation({ state: rawState });
    assert.deepEqual(presentation, generic, `${rawState} must use the generic state`);
    assert.equal(JSON.stringify(presentation).includes(rawState), false);
  }

  for (const presentation of [ok, generic]) {
    assert.ok(["ok", "warn", "bad"].includes(presentation.kind));
    assert.equal(typeof messages.en[presentation.labelKey], "string");
    assert.equal(typeof messages.zh[presentation.labelKey], "string");
  }
});


test("the state pill renders the helper's localized label instead of raw item.state", () => {
  const pill = jsxElements("span").find((element) =>
    element.openingElement.getText(pageFile).includes("pill pill-"),
  );
  assert.ok(pill, "source rows must retain a status pill");
  assert.doesNotMatch(
    pill.getText(pageFile),
    /\{\s*item\.state/,
    "provider-controlled item.state must never be rendered directly",
  );

  const localizedLabel = descendants(
    pill,
    (node) =>
      ts.isElementAccessExpression(node) &&
      node.expression.getText(pageFile) === "t" &&
      ts.isPropertyAccessExpression(node.argumentExpression) &&
      node.argumentExpression.name.text === "labelKey",
  )[0];
  assert.ok(
    localizedLabel,
    "the pill must resolve its allowlisted labelKey through the active locale",
  );
});


test("source identity is stable and collision-free for missing or duplicate ids", () => {
  assert.equal(
    typeof viewState.dataHealthSourceKey,
    "function",
    "extract canonical row/query identity into the pure dataHealthSourceKey helper",
  );

  const duplicate = { providerId: "same-provider", sourceId: "same-source" };
  const duplicateKeys = [
    viewState.dataHealthSourceKey(duplicate, 0),
    viewState.dataHealthSourceKey(duplicate, 1),
  ];
  assert.notEqual(
    duplicateKeys[0],
    duplicateKeys[1],
    "duplicate upstream ids still need unique React/deep-link identities",
  );

  const missingKeys = [
    viewState.dataHealthSourceKey({}, 0),
    viewState.dataHealthSourceKey({}, 1),
    viewState.dataHealthSourceKey({ providerId: "missing-source" }, 2),
    viewState.dataHealthSourceKey({ sourceId: "missing-provider" }, 3),
  ];
  assert.equal(new Set(missingKeys).size, missingKeys.length);

  for (const [item, index, key] of [
    [duplicate, 0, duplicateKeys[0]],
    [{}, 0, missingKeys[0]],
  ]) {
    assert.equal(typeof key, "string");
    assert.ok(key.length > 0);
    assert.equal(
      viewState.dataHealthSourceKey(item, index),
      key,
      "the same snapshot row must retain one stable key",
    );
  }
});


test("React expansion and the source query param share the canonical source key", () => {
  assert.doesNotMatch(
    pageSource,
    /function\s+sourceKey\s*\(/,
    "DataHealthPage must not keep a divergent local identity builder",
  );
  assert.doesNotMatch(
    pageSource,
    /setSearchParams\s*\(\s*\{\s*source:\s*`/,
    "the query param must not be independently rebuilt from provider/source fields",
  );

  const keyCalls = descendants(
    pageFile,
    (node) =>
      ts.isCallExpression(node) &&
      node.expression.getText(pageFile) === "dataHealthSourceKey",
  );
  assert.ok(
    keyCalls.length >= 3,
    "deep-link matching, expansion, and row rendering must share dataHealthSourceKey",
  );
  for (const call of keyCalls) {
    assert.equal(
      call.arguments.length,
      2,
      "canonical source keys need the snapshot index to disambiguate missing/duplicate ids",
    );
  }

  const toggle = descendants(
    pageFile,
    (node) => ts.isFunctionDeclaration(node) && node.name?.text === "toggle",
  )[0];
  assert.ok(toggle, "source expansion must retain a toggle action");
  assert.match(
    toggle.getText(pageFile),
    /setSearchParams\s*\(\s*\{\s*source:\s*key\s*\}/,
    "the exact expansion key must become the source query value",
  );
});


test("accordion controls use opaque per-row ids whose target persists while collapsed", () => {
  const controls = descendants(
    pageFile,
    (node) =>
      ts.isJsxAttribute(node) && node.name.text === "aria-controls",
  );
  assert.equal(controls.length, 1, "each source-row template needs one control target");

  const controlsOpening = controls[0].parent.parent;
  assert.ok(ts.isJsxOpeningElement(controlsOpening));
  const controlsExpression = jsxAttributeExpression(
    controlsOpening,
    "aria-controls",
  );
  assert.ok(
    controlsExpression && ts.isIdentifier(controlsExpression),
    "aria-controls must reference a shared opaque id variable",
  );

  const rowFunction = nearestNamedFunction(controls[0]);
  assert.ok(rowFunction, "the accordion row must be a named component");
  assert.notEqual(
    functionName(rowFunction),
    "DataHealthPage",
    "each row needs its own component scope so useId remains stable and unique",
  );

  const idName = controlsExpression.text;
  const idTarget = descendants(
    rowFunction,
    (node) =>
      ts.isJsxElement(node) &&
      jsxAttributeExpression(node.openingElement, "id")?.getText(pageFile) ===
        idName,
  )[0];
  assert.ok(idTarget, "aria-controls must reference a real detail element id");

  const idDerivation = resolvedInitializerText(idName, rowFunction);
  assert.match(idDerivation, /\buseId\s*\(/, "row ids must originate from React useId");
  assert.doesNotMatch(
    idDerivation,
    /providerId|sourceId|\bkey\b/,
    "DOM ids must not contain external provider/source identifiers",
  );

  const hidden = jsxAttributeExpression(idTarget.openingElement, "hidden");
  assert.equal(
    hidden?.getText(pageFile).replaceAll(" ", ""),
    "!open",
    "the detail target must stay mounted and become hidden while collapsed",
  );
  for (let current = idTarget.parent; current !== rowFunction; current = current.parent) {
    if (ts.isConditionalExpression(current)) {
      assert.doesNotMatch(
        current.condition.getText(pageFile),
        /\bopen\b/,
        "the controlled detail target must not be conditionally removed",
      );
    }
  }
});


test("Show all sources moves focus to the selected All sources filter", () => {
  const allSourcesButton = jsxElementWithText("button", "t.allSources");
  assert.ok(allSourcesButton, "All sources filter button must exist");
  assert.equal(
    jsxAttributeExpression(allSourcesButton.openingElement, "ref")?.getText(
      pageFile,
    ),
    "allSourcesButtonRef",
  );

  const showAllButton = jsxElementWithText("button", "t.showAllSources");
  assert.ok(showAllButton, "filtered-empty recovery button must exist");
  assert.equal(
    jsxAttributeExpression(showAllButton.openingElement, "onClick")?.getText(
      pageFile,
    ),
    "showAllSources",
  );

  const showAllSources = descendants(
    pageFile,
    (node) =>
      ts.isFunctionDeclaration(node) && node.name?.text === "showAllSources",
  )[0];
  assert.ok(showAllSources, "focus transfer must live in the Show all action");
  const actionSource = showAllSources.getText(pageFile);
  const selectIndex = actionSource.indexOf('setFilter("all")');
  const frameIndex = actionSource.indexOf("requestAnimationFrame");
  const focusIndex = actionSource.indexOf("allSourcesButtonRef.current?.focus()");
  assert.ok(selectIndex >= 0, "Show all must select the All sources filter");
  assert.ok(frameIndex > selectIndex, "focus must wait until the filter update commits");
  assert.ok(focusIndex > frameIndex, "the selected All sources button must receive focus");
});


test("Data Health copy stays in parity across English and Chinese", () => {
  assert.deepEqual(
    Object.keys(messages.en).sort(),
    Object.keys(messages.zh).sort(),
    "English and Chinese message catalogs must expose identical keys",
  );

  for (const key of ["noSourceIssues", "showAllSources", "healthLoadError"]) {
    assert.equal(typeof messages.en[key], "string", `missing English ${key}`);
    assert.ok(messages.en[key].trim(), `empty English ${key}`);
    assert.equal(typeof messages.zh[key], "string", `missing Chinese ${key}`);
    assert.ok(messages.zh[key].trim(), `empty Chinese ${key}`);
  }
});
