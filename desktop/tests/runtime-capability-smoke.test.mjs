import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { validateRuntimeCapabilitySmoke } from "../scripts/smoke_runtime_capability.mjs";

const GENERIC_ERROR = "desktop runtime capability smoke failed";
const DESKTOP_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const execFileAsync = promisify(execFile);
const FEATURE_IDS = [
  "listener",
  "should_send",
  "responses",
  "cache",
  "fallback",
  "pii_redaction",
  "streaming",
];

function closedReadyCapability() {
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: {
      operational: "ready",
      generatedAt: "2026-08-09T01:02:03Z",
      lastGoodAt: "2026-08-09T01:02:03Z",
      dataRevision: 17,
      schemaVersion: "1.0",
    },
    gateway: {
      mode: "gateway",
      operational: "ready",
      features: Object.fromEntries(
        FEATURE_IDS.map((featureId) => [
          featureId,
          {
            support: "supported",
            enabled: true,
            configured: true,
            operational: "ready",
          },
        ]),
      ),
      configuredProviderCount: 1,
      healthyProviderCount: 1,
      actions: ["retry"],
      lastError: null,
    },
  };
}

function assertGenericFailure(payload, forbiddenValues = [], canaries = []) {
  assert.throws(
    () => validateRuntimeCapabilitySmoke(payload, forbiddenValues),
    (error) => {
      assert.equal(error?.name, "Error");
      assert.equal(error?.message, GENERIC_ERROR);
      for (const canary of canaries) {
        assert.equal(error.message.includes(canary), false);
      }
      return true;
    },
  );
}

function fakeCollectorSource() {
  return `#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const capturePath = process.env.OPENUSAGE_TEST_CAPTURE;
const home = process.env.HOME;
const userProfile = process.env.USERPROFILE;
const localAppData = process.env.LOCALAPPDATA;
const runtimeRoot = typeof home === "string" ? path.dirname(home) : "";
const socketPath = path.join(
  home || "",
  ".local",
  "state",
  "openusage-bar",
  "openusage.sock",
);
const expectedArguments = [
  "--offline",
  "daemon",
  "--interval",
  "300",
  "--api-transport",
  "unix",
  "--api-socket",
  socketPath,
];

function writeCapture(phase, receivedSignal = null) {
  fs.writeFileSync(
    capturePath,
    JSON.stringify({
      schemaVersion: "fake-collector-capture/v1",
      phase,
      receivedSignal,
      pid: process.pid,
      runtimeRoot,
      socketPath,
      arguments: process.argv.slice(2),
      environment: { home, userProfile, localAppData },
    }),
    { mode: 0o600 },
  );
}

function fail() {
  try {
    writeCapture("failed");
  } finally {
    process.exit(70);
  }
}

const runtimeMetadata = fs.lstatSync(runtimeRoot);
if (
  typeof capturePath !== "string" ||
  !path.isAbsolute(capturePath) ||
  runtimeRoot.length === 0 ||
  !path.basename(runtimeRoot).startsWith("openusage-smoke-") ||
  !runtimeMetadata.isDirectory() ||
  (runtimeMetadata.mode & 0o777) !== 0o700 ||
  home !== path.join(runtimeRoot, "home") ||
  userProfile !== path.join(runtimeRoot, "profile") ||
  localAppData !== path.join(runtimeRoot, "local-app-data") ||
  JSON.stringify(process.argv.slice(2)) !== JSON.stringify(expectedArguments)
) {
  fail();
}

const socketParent = path.dirname(socketPath);
fs.mkdirSync(socketParent, { recursive: true, mode: 0o700 });
fs.chmodSync(socketParent, 0o700);

const health = Buffer.from(
  JSON.stringify({
    schemaVersion: "1.0",
    dataRevision: 17,
    generatedAt: "2026-08-09T01:02:03Z",
    health: { ok: true, status: "ok" },
  }),
  "utf8",
);
const server = http.createServer((request, response) => {
  if (request.method !== "GET" || request.url !== "/v1/health") {
    response.writeHead(404, { "Content-Length": "0" });
    response.end();
    return;
  }
  response.writeHead(200, {
    "Cache-Control": "no-store",
    "Content-Length": String(health.length),
    "Content-Type": "application/json; charset=utf-8",
  });
  response.end(health);
});

server.on("error", fail);
process.once("SIGTERM", () => {
  writeCapture("terminated", "SIGTERM");
  try {
    server.close();
  } finally {
    process.exit(0);
  }
});
server.listen(socketPath, () => {
  fs.chmodSync(socketPath, 0o600);
  const socketParentMetadata = fs.lstatSync(socketParent);
  const socketMetadata = fs.lstatSync(socketPath);
  if (
    !socketParentMetadata.isDirectory() ||
    (socketParentMetadata.mode & 0o777) !== 0o700 ||
    !socketMetadata.isSocket() ||
    (socketMetadata.mode & 0o777) !== 0o600
  ) {
    fail();
  }
  writeCapture("running");
});
`;
}

function makeFakeCollector(testContext) {
  const fixtureRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "openusage-cli-contract-"),
  );
  testContext.after(() => {
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  });
  const collectorPath = path.join(fixtureRoot, "fake-collector");
  const capturePath = path.join(fixtureRoot, "capture.json");
  fs.writeFileSync(collectorPath, fakeCollectorSource(), { mode: 0o700 });
  fs.chmodSync(collectorPath, 0o700);
  return { fixtureRoot, collectorPath, capturePath };
}

test("returns only the safe aggregate for a closed ready renderer capability", () => {
  assert.deepEqual(validateRuntimeCapabilitySmoke(closedReadyCapability()), {
    schemaVersion: "desktop-runtime-capability-smoke/v1",
    object: "desktop.runtime_capability_smoke",
    ok: true,
    observerOperational: "ready",
    gatewayMode: "gateway",
    gatewayOperational: "ready",
  });
});

test("uses one generic error for malformed, not-ready, or non-allowlisted capability state", () => {
  assertGenericFailure(null);

  const notReady = closedReadyCapability();
  notReady.observer.operational = "degraded";
  assertGenericFailure(notReady);

  const futureGateway = closedReadyCapability();
  futureGateway.gateway.mode = "private-forwarder";
  assertGenericFailure(futureGateway);
});

test("rejects additive private fields and forbidden paths or tokens without reflection", () => {
  const additiveCanary = "ADDITIVE_PRIVATE_CANARY_9b78d7";
  const withAdditivePrivateField = closedReadyCapability();
  withAdditivePrivateField.privateCanary = additiveCanary;
  assertGenericFailure(withAdditivePrivateField, [], [additiveCanary]);

  const pathCanary = "/Users/private/PATH_CANARY_71c40f/runtime.sock";
  const withForbiddenPath = closedReadyCapability();
  withForbiddenPath.gateway.privatePath = pathCanary;
  assertGenericFailure(withForbiddenPath, [pathCanary], [pathCanary]);

  const tokenCanary = "sk-private-TOKEN_CANARY_c4469e";
  const withForbiddenToken = closedReadyCapability();
  withForbiddenToken.gateway.bearerToken = tokenCanary;
  assertGenericFailure(withForbiddenToken, [tokenCanary], [tokenCanary]);
});

test(
  "runs the built-collector CLI seam offline and removes its private runtime",
  {
    skip:
      process.platform === "win32"
        ? "real Windows process coverage runs in the hosted native smoke"
        : false,
    timeout: 15_000,
  },
  async (testContext) => {
    const { fixtureRoot, collectorPath, capturePath } =
      makeFakeCollector(testContext);
    const tokenCanary = "sk-private-CLI_TOKEN_CANARY_52d19c";
    const smokeScript = path.join(
      DESKTOP_ROOT,
      "scripts",
      "smoke_runtime_capability.mjs",
    );

    const { stdout, stderr } = await execFileAsync(
      process.execPath,
      [smokeScript, "--collector", collectorPath],
      {
        cwd: DESKTOP_ROOT,
        encoding: "utf8",
        env: {
          ...process.env,
          OPENUSAGE_TEST_CAPTURE: capturePath,
          OPENUSAGE_TEST_TOKEN_CANARY: tokenCanary,
        },
        maxBuffer: 64 * 1024,
        timeout: 12_000,
      },
    );

    assert.equal(stderr, "");
    assert.equal(stdout.endsWith("\n"), true);
    assert.equal(stdout.trim().split("\n").length, 1);
    assert.deepEqual(JSON.parse(stdout), {
      schemaVersion: "desktop-runtime-capability-smoke/v1",
      object: "desktop.runtime_capability_smoke",
      ok: true,
      observerOperational: "ready",
      gatewayMode: "unknown",
      gatewayOperational: "unavailable",
    });

    const capture = JSON.parse(fs.readFileSync(capturePath, "utf8"));
    assert.equal(capture.schemaVersion, "fake-collector-capture/v1");
    assert.equal(capture.phase, "terminated");
    assert.equal(capture.receivedSignal, "SIGTERM");
    assert.equal(Number.isInteger(capture.pid) && capture.pid > 0, true);
    assert.throws(
      () => process.kill(capture.pid, 0),
      (error) => error?.code === "ESRCH",
    );
    assert.equal(
      path.basename(capture.runtimeRoot).startsWith("openusage-smoke-"),
      true,
    );
    assert.deepEqual(capture.environment, {
      home: path.join(capture.runtimeRoot, "home"),
      userProfile: path.join(capture.runtimeRoot, "profile"),
      localAppData: path.join(capture.runtimeRoot, "local-app-data"),
    });
    assert.deepEqual(capture.arguments, [
      "--offline",
      "daemon",
      "--interval",
      "300",
      "--api-transport",
      "unix",
      "--api-socket",
      capture.socketPath,
    ]);
    assert.equal(capture.socketPath.startsWith(capture.runtimeRoot), true);
    assert.equal(fs.existsSync(capture.runtimeRoot), false);

    for (const forbidden of [
      fixtureRoot,
      collectorPath,
      capturePath,
      capture.runtimeRoot,
      capture.socketPath,
      tokenCanary,
    ]) {
      assert.equal(stdout.includes(forbidden), false);
      assert.equal(stderr.includes(forbidden), false);
    }
  },
);

test("invalid collector arguments and paths expose only the generic boundary error", async (testContext) => {
  const fixtureRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "openusage-cli-error-contract-"),
  );
  testContext.after(() => {
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  });
  const smokeScript = path.join(
    DESKTOP_ROOT,
    "scripts",
    "smoke_runtime_capability.mjs",
  );
  const pathCanary = path.join(
    fixtureRoot,
    "MISSING_COLLECTOR_PATH_CANARY_220b4f",
  );
  const argumentCanary = "--COLLECTOR_ARGUMENT_CANARY_4bec16";

  for (const arguments_ of [
    [smokeScript, argumentCanary, pathCanary],
    [smokeScript, "--collector", pathCanary],
  ]) {
    await assert.rejects(
      execFileAsync(process.execPath, arguments_, {
        cwd: DESKTOP_ROOT,
        encoding: "utf8",
        maxBuffer: 64 * 1024,
        timeout: 5_000,
      }),
      (error) => {
        assert.equal(error?.code, 1);
        assert.equal(error?.stdout, "");
        assert.equal(error?.stderr, `${GENERIC_ERROR}\n`);
        assert.equal(error.stderr.includes(pathCanary), false);
        assert.equal(error.stderr.includes(argumentCanary), false);
        return true;
      },
    );
  }
});
