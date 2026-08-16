import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const providersSource = await readFile(
  new URL("../src/pages/ProvidersPage.tsx", import.meta.url),
  "utf8",
);
const actionsSource = await readFile(
  new URL("../src/components/ProviderAccountActions.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const addProviderSource = await readFile(
  new URL("../src/components/AddProviderDialog.tsx", import.meta.url),
  "utf8",
);
const i18nSource = await readFile(
  new URL("../src/i18n.ts", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);

test("Gateway account actions live in Account Pools and never in Observer preset browsing", () => {
  assert.match(providersSource, /<ProviderAccountActions/);
  assert.match(providersSource, /<GatewayAccountCreateDialog/);
  assert.match(providersSource, /AccountPoolAccountCard/);
  assert.match(providersSource, /gatewayAccount\.openCreate/);
  assert.doesNotMatch(addProviderSource, /gatewayAccount\.open(?:Create|Edit|Replace|Remove)/);
  assert.doesNotMatch(addProviderSource, /GatewayAccountHostAdapter/);
  assert.match(i18nSource, /addGatewayAccount:/);
  assert.match(providersSource, /<dd>\{account\.providerId\}<\/dd>/);
});

test("standalone Web renders read-only guidance and no executable Gateway account CTA", () => {
  assert.match(providersSource, /normalizeGatewayAccountCapability/);
  assert.match(providersSource, /gatewayAccountHostAvailability === "trusted"/);
  assert.match(actionsSource, /if \(!trusted\) return null/);
  assert.match(providersSource, /t\.gatewayAccountReadOnly/);
  assert.match(i18nSource, /gatewayAccountReadOnly:\s*\n?\s*"[^"]*read-only/i);
  assert.match(providersSource, /gatewayAccountHostAvailability === "checking"/);
  assert.match(providersSource, /t\.gatewayAccountChecking/);
});

test("local Gateway account work disables every host CTA until the operation is terminal", () => {
  assert.match(providersSource, /gatewayAccountBusy=/);
  assert.match(actionsSource, /disabled=\{busy\}/);
  assert.match(providersSource, /disabled=\{gatewayAccountBusy\}/);
});

test("create UI exposes only the four Gateway egress providers and a secure-window continuation", () => {
  assert.match(actionsSource, /GATEWAY_PROVIDER_PRESETS\.map/);
  for (const preset of ["OpenAI", "Anthropic", "DeepSeek", "OpenRouter"]) {
    assert.match(actionsSource, new RegExp(`"${preset.toLowerCase()}"|>${preset}<|return "${preset}"`, "i"));
  }
  for (const observerOnly of ["MiniMax", "Moonshot", "Codex", "step_plan"]) {
    assert.doesNotMatch(actionsSource, new RegExp(observerOnly, "i"));
  }
  assert.match(actionsSource, /t\.continueSecureWindow/);
  assert.match(i18nSource, /continueSecureWindow: "Continue in secure window"/);
  assert.match(i18nSource, /continueSecureWindow: "在安全窗口中继续"/);
  assert.doesNotMatch(actionsSource, /type=["']password["']|credential\s*=|token\s*=/i);
});

test("operation lifecycle is allowlisted, polled without mutation retry, and refresh failure stays separate", () => {
  assert.match(providersSource, /normalizeGatewayAccountOperationResponse/);
  assert.match(providersSource, /normalizeGatewayAccountOperationStatus/);
  assert.match(providersSource, /readOperation\(gatewayAccountOperation\.operationId/);
  assert.match(providersSource, /status\.state === "succeeded"/);
  assert.match(providersSource, /setGatewayPublicListRefreshFailed\(true\)/);
  assert.doesNotMatch(providersSource, /launchGatewayAccountIntent\([^)]*\)\s*;?\s*launchGatewayAccountIntent/s);
  assert.match(providersSource, /code === "account_in_use"/);
  assert.match(i18nSource, /Remove it from every Pool before trying again/);
});

test("secure dialog traps focus, supports Escape and restores focus", () => {
  assert.match(actionsSource, /role="dialog"/);
  assert.match(actionsSource, /aria-modal="true"/);
  assert.match(actionsSource, /requestAnimationFrame\(\(\) => initialFocusRef\.current\?\.focus\(\)\)/);
  assert.match(actionsSource, /event\.key === "Escape"/);
  assert.match(actionsSource, /event\.key !== "Tab"/);
  assert.match(actionsSource, /trigger\?\.focus\(\)/);
});

test("Gateway account controls remain usable from 320 through desktop widths", () => {
  assert.match(cssSource, /\.gateway-account-dialog\s*\{[^}]*100dvh/s);
  assert.match(cssSource, /\.account-pools-head-actions\s*\{/);
  assert.match(cssSource, /@media \(max-width: 640px\)[\s\S]*\.provider-account-actions\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(cssSource, /@media \(max-width: 640px\)[\s\S]*\.gateway-account-dialog\s*\{[^}]*calc\(100vw - 16px\)/);
});

test("Observer preset dialog follows the preset-form-save flow, never endpoint/credential health", () => {
  assert.match(addProviderSource, /providerConfigSave/);
  assert.match(addProviderSource, /providerConfigBaseUrl/);
  assert.match(addProviderSource, /providerConfigModel/);
  assert.match(providersSource, /applyProviderConfig/);
  assert.match(providersSource, /fetchProviderConfigPresets/);
  assert.match(addProviderSource, /preset-category-tabs/);
  assert.doesNotMatch(addProviderSource, /testEndpoint/);
  assert.doesNotMatch(addProviderSource, /checkConsoleReachability/);
  assert.match(i18nSource, /providerConfigSave: "Save to Agent config"/);
  assert.match(i18nSource, /providerConfigDesktopRequired: "Saving requires the UsageHub desktop app\."/);
});
