import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const providersSource = await readFile(
  new URL("../src/pages/ProvidersPage.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);
const i18nSource = await readFile(new URL("../src/i18n.ts", import.meta.url), "utf8");
const editorSource = await readFile(
  new URL("../src/components/AccountPoolDialogs.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const providersFile = ts.createSourceFile(
  "ProvidersPage.tsx",
  providersSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

const ACCOUNT_POOL_COPY = [
  "accountPoolsTitle",
  "accountPoolsPrivacyNote",
  "accountPoolsSummary",
  "accountPoolsEmpty",
  "accountPoolsUnknown",
  "createPool",
  "createPoolUnverified",
  "accountPoolStatusReady",
  "accountPoolStatusCooldown",
  "accountPoolStatusQuotaExhausted",
  "accountPoolStatusDisabled",
  "accountPoolStatusBackendUnavailable",
  "accountPoolStatusUnknown",
  "priority",
  "weight",
  "cooldown",
  "quotaState",
  "accountPoolMembership",
];

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
    providersFile,
    (node) =>
      ts.isJsxElement(node) &&
      node.openingElement.tagName.getText(providersFile) === tagName,
  );
}

function jsxSelfClosing(tagName) {
  return descendants(
    providersFile,
    (node) =>
      ts.isJsxSelfClosingElement(node) &&
      node.tagName.getText(providersFile) === tagName,
  );
}

function hasAttribute(opening, name, pattern) {
  const attribute = opening.attributes.properties.find(
    (item) => ts.isJsxAttribute(item) && item.name.text === name,
  );
  if (!attribute) return false;
  if (!pattern) return true;
  return pattern.test(attribute.getText(providersFile));
}

test("Providers embeds account pools without adding a top-level route", () => {
  assert.match(
    providersSource,
    /from\s+["']\.\.\/accountPools["']/,
    "Providers must consume the renderer-safe account pool model",
  );
  assert.match(providersSource, /accountPoolsViewModel\(accountPoolsSnapshot\)/);
  assert.match(providersSource, /<AccountPoolsSection\s+t=\{t\}\s+model=\{accountPools\}/);
  assert.match(providersSource, /<section\s+className="provider-grid"/);
  assert.doesNotMatch(
    providersSource,
    /<Route|createBrowserRouter|path:\s*["'][^"']*pool/i,
    "account pools stay inside Providers instead of becoming a new page",
  );
});

test("account pool fetch is credential-free and fails closed to unknown UI", () => {
  assert.match(providersSource, /const\s+ACCOUNT_POOLS_PATH\s*=\s*["']\/gateway\/v1\/account-pools["']/);
  assert.match(providersSource, /credentials:\s*["']omit["']/);
  assert.match(providersSource, /cache:\s*["']no-store["']/);
  assert.match(providersSource, /if\s*\(!response\.ok\)\s*return\s+null/);
  assert.doesNotMatch(providersSource, /\bheaders\s*:/);
  assert.doesNotMatch(providersSource, /\bbody\s*:/);
  assert.doesNotMatch(
    providersSource,
    /credentialStoreAccount|externalOpaqueRef|accountRef|secretToken|privateRuntimePath/i,
    "Providers must not render private account pool fields",
  );
});

test("renders alias or display ID with priority, weight, cooldown, quota, and memberships", () => {
  assert.match(providersSource, /account\.alias\s*\?\?\s*account\.displayId/);
  assert.match(providersSource, /account\.priority/);
  assert.match(providersSource, /account\.weight/);
  assert.match(providersSource, /cooldownText\(account,\s*t\)/);
  assert.match(providersSource, /quotaText\(account,\s*t\)/);
  assert.match(providersSource, /account\.pools\.length/);
  assert.match(providersSource, /accountPoolMembership/);
});

test("separates ready, cooldown, exhausted, disabled, backend unavailable, and unknown copy", () => {
  for (const key of ACCOUNT_POOL_COPY) {
    assert.match(i18nSource, new RegExp(`${key}:`), `missing copy key ${key}`);
  }
  assert.match(i18nSource, /accountPoolStatusDisabled:\s*"Disabled"/);
  assert.match(i18nSource, /accountPoolStatusBackendUnavailable:\s*"Backend unavailable"/);
  assert.match(i18nSource, /accountPoolStatusUnknown:\s*"Unknown"/);
  assert.match(providersSource, /t\[account\.statusKey\]/);
  assert.match(providersSource, /poolStatusClass\(account\.statusTone\)/);
});

test("Create Pool copy does not claim health or quota verification", () => {
  const createCopy = /createPoolUnverified:\s*"([^"]+)"/.exec(i18nSource)?.[1] ?? "";
  assert.match(createCopy, /unverified/i);
  assert.match(createCopy, /does not prove account health or quota/i);
  assert.doesNotMatch(createCopy, /\bhealthy\b/i);
  assert.doesNotMatch(createCopy, /\bverified quota\b/i);
  assert.match(providersSource, /disabled=\{hostAvailability\s*!==\s*["']trusted["']/);
  assert.match(providersSource, /account-pool-create-note/);
  assert.match(i18nSource, /accountPoolsReadOnly:/);
});

test("trusted host capability exclusively gates create, edit, and remove actions", () => {
  assert.match(providersSource, /normalizeTrustedHostCapability/);
  assert.match(providersSource, /hostAvailability/);
  assert.match(providersSource, /model\.pools\.map/);
  assert.match(providersSource, /<AccountPoolDefinitionCard/);
  assert.match(providersSource, /hostAvailability\s*===\s*["']trusted["']/);
  assert.match(providersSource, /accountPool\.create/);
  assert.match(providersSource, /accountPool\.edit/);
  assert.match(providersSource, /accountPool\.remove/);
  assert.doesNotMatch(
    providersSource + editorSource,
    /credentialStoreAccount|externalOpaqueRef|accountId|secretToken|privateRuntimePath/i,
  );
});

test("Pool dialogs expose bounded keyboard, cancellation, CAS, and safe error states", () => {
  assert.match(editorSource, /role="dialog"/);
  assert.match(editorSource, /aria-modal="true"/);
  assert.match(editorSource, /event\.key\s*===\s*["']Escape["']/);
  assert.match(editorSource, /trigger\?\.focus/);
  assert.match(editorSource, /aria-invalid/);
  assert.match(editorSource, /role="alert"/);
  assert.match(providersSource, /revision_conflict/);
  assert.match(providersSource, /refreshFailed/);
  assert.doesNotMatch(
    providersSource,
    /revision_conflict[\s\S]{0,300}(retry|expectedRevision\s*\+)/i,
    "revision conflicts must not be automatically retried or advanced",
  );
});

test("account pool cards are keyboard reachable and ARIA described", () => {
  const sections = jsxElements("section");
  const accountPoolSection = sections.find((section) =>
    /account-pools-panel/.test(section.openingElement.getText(providersFile)),
  );
  assert.ok(accountPoolSection, "render account pools section");
  assert.ok(hasAttribute(accountPoolSection.openingElement, "aria-labelledby", /account-pools-title/));
  assert.ok(hasAttribute(accountPoolSection.openingElement, "aria-describedby", /describedBy|account-pools-note/));
  assert.match(providersSource, /const\s+describedBy\s*=\s*"account-pools-note"/);

  const list = jsxElements("div").find((node) =>
    /className="account-pools-list"/.test(node.openingElement.getText(providersFile)),
  );
  assert.ok(list, "render an account pool list container");
  assert.ok(hasAttribute(list.openingElement, "role", /list/));

  const article = jsxElements("article").find((node) =>
    /account-pool-card/.test(node.openingElement.getText(providersFile)),
  );
  assert.ok(article, "render account pool account cards");
  assert.ok(hasAttribute(article.openingElement, "role", /listitem/));
  assert.ok(hasAttribute(article.openingElement, "tabIndex", /\{0\}/));
  assert.ok(hasAttribute(article.openingElement, "aria-labelledby", /titleId/));
  assert.ok(hasAttribute(article.openingElement, "aria-describedby", /detailId/));

  const statusRegions = jsxSelfClosing("span").filter((node) =>
    /provider-status-dot/.test(node.getText(providersFile)),
  );
  assert.ok(statusRegions.some((node) => hasAttribute(node, "aria-hidden", /true/)));
});

test("account pool styles cover 320px mobile through 1440px desktop without overlap", () => {
  assert.match(cssSource, /\.account-pools-list\s*\{[\s\S]*?grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(280px,\s*1fr\)\)/);
  assert.match(cssSource, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.account-pools-list\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(cssSource, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.account-pool-card-main\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(cssSource, /\.account-pool-card:focus-visible\s*\{[\s\S]*?box-shadow:\s*0 0 0 3px var\(--accent-soft\)/);
  assert.match(cssSource, /\.account-pool-display-id\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
  assert.match(cssSource, /\.account-pool-memberships\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
  assert.match(cssSource, /\.account-pool-dialog\s*\{[\s\S]*?100dvh/);
  assert.match(cssSource, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.account-pool-member-row\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(cssSource, /\.account-pool-dialog-actions\s+button\s*\{[\s\S]*?min-height:\s*44px/);
});
