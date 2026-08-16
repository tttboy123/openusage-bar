import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs";
import test from "node:test";

const require = createRequire(import.meta.url);

test("resolves the packaged settings helper for host actions on all desktop platforms", () => {
  const { resolveHostActionExecutor } = require("../settings_runtime.js");
  const existing = new Set([
    "/Applications/UsageHub.app/Contents/Resources/settings/openusage-settings",
    "C:\\Program Files\\UsageHub\\resources\\settings\\openusage-settings.exe",
    "/opt/UsageHub/resources/settings/openusage-settings",
  ]);
  const pathExists = (candidate) => existing.has(candidate);

  assert.deepEqual(
    resolveHostActionExecutor({
      isPackaged: true,
      resourcesPath: "/Applications/UsageHub.app/Contents/Resources",
      platform: "darwin",
      pathExists,
    }),
    {
      command: "/Applications/UsageHub.app/Contents/Resources/settings/openusage-settings",
      args: ["gateway-account-mutate"],
    },
  );
  assert.deepEqual(
    resolveHostActionExecutor({
      isPackaged: true,
      resourcesPath: "C:\\Program Files\\UsageHub\\resources",
      platform: "win32",
      pathExists,
    }),
    {
      command:
        "C:\\Program Files\\UsageHub\\resources\\settings\\openusage-settings.exe",
      args: ["gateway-account-mutate"],
    },
  );
  assert.deepEqual(
    resolveHostActionExecutor({
      isPackaged: true,
      resourcesPath: "/opt/UsageHub/resources",
      platform: "linux",
      pathExists,
    }),
    {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-mutate"],
    },
  );
});

test("resolves the packaged Gateway account editor with a fixed mode on all desktop platforms", () => {
  const { resolveGatewayAccountEditorExecutor } = require("../settings_runtime.js");
  const existing = new Set([
    "/Applications/UsageHub.app/Contents/Resources/settings/openusage-settings",
    "C:\\Program Files\\UsageHub\\resources\\settings\\openusage-settings.exe",
    "/opt/UsageHub/resources/settings/openusage-settings",
  ]);
  const pathExists = (candidate) => existing.has(candidate);

  for (const [platform, resourcesPath, command] of [
    ["darwin", "/Applications/UsageHub.app/Contents/Resources", "/Applications/UsageHub.app/Contents/Resources/settings/openusage-settings"],
    ["win32", "C:\\Program Files\\UsageHub\\resources", "C:\\Program Files\\UsageHub\\resources\\settings\\openusage-settings.exe"],
    ["linux", "/opt/UsageHub/resources", "/opt/UsageHub/resources/settings/openusage-settings"],
  ]) {
    assert.deepEqual(
      resolveGatewayAccountEditorExecutor({
        isPackaged: true,
        resourcesPath,
        platform,
        pathExists,
      }),
      { command, args: ["gateway-account-editor"] },
    );
  }
});

test("host action resolver fails closed in development and never falls back to collector", () => {
  const { resolveHostActionExecutor } = require("../settings_runtime.js");
  const existing = new Set([
    "/repo/dist-collector/openusage-collector",
    "/repo/resources/collector/openusage-collector",
    "/repo/resources/settings/openusage-settings",
  ]);
  const pathExists = (candidate) => existing.has(candidate);

  assert.equal(
    resolveHostActionExecutor({
      isPackaged: false,
      resourcesPath: "/repo/resources",
      platform: "linux",
      pathExists,
      environment: {
        USAGEHUB_COLLECTOR: "/repo/dist-collector/openusage-collector",
        USAGEHUB_HOST_ACTION: "/repo/resources/settings/openusage-settings",
      },
    }),
    null,
  );
  assert.equal(
    resolveHostActionExecutor({
      isPackaged: true,
      resourcesPath: "/repo/resources",
      platform: "linux",
      pathExists: (candidate) => candidate.endsWith("/collector/openusage-collector"),
    }),
    null,
  );
  assert.equal(
    resolveHostActionExecutor({
      isPackaged: true,
      resourcesPath: "/repo/../resources",
      platform: "linux",
      pathExists,
    }),
    null,
  );
});

test("desktop package declares the settings runtime and three packaged helper resources", () => {
  const packageJson = require("../package.json");
  assert.equal(packageJson.build.files.includes("settings_runtime.js"), true);
  assert.equal(packageJson.build.files.includes("gateway_account_operations.js"), true);
  assert.deepEqual(
    packageJson.build.mac.extraResources.find((item) => item.to === "settings/openusage-settings"),
    {
      from: "../dist-settings/openusage-settings",
      to: "settings/openusage-settings",
    },
  );
  assert.deepEqual(
    packageJson.build.win.extraResources.find((item) => item.to === "settings/openusage-settings.exe"),
    {
      from: "../dist-settings/openusage-settings.exe",
      to: "settings/openusage-settings.exe",
    },
  );
  assert.deepEqual(
    packageJson.build.linux.extraResources.find((item) => item.to === "settings/openusage-settings"),
    {
      from: "../dist-settings/openusage-settings",
      to: "settings/openusage-settings",
    },
  );
});

test("desktop main wires the packaged Gateway account editor into the same-origin host", () => {
  const main = fs.readFileSync(new URL("../main.js", import.meta.url), "utf8");
  assert.match(main, /resolveGatewayAccountEditorExecutor/u);
  assert.match(main, /gatewayAccountEditorExecutor:\s*resolveGatewayAccountEditorExecutor/u);
});

test("resolves a packaged provider-config helper executor", () => {
  const {
    resolveProviderConfigExecutor,
  } = require("../settings_runtime.js");
  const executor = resolveProviderConfigExecutor({
    isPackaged: true,
    resourcesPath: "/Applications/UsageHub.app/Contents/Resources",
    platform: "darwin",
    pathExists: () => true,
  });
  assert.deepEqual(executor, {
    command:
      "/Applications/UsageHub.app/Contents/Resources/settings/openusage-settings",
    args: ["provider-config-apply"],
  });
  assert.equal(
    resolveProviderConfigExecutor({ isPackaged: false, resourcesPath: "/x", pathExists: () => true }),
    null,
  );
});
