#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { normalizeRuntimeCapability } = require("../runtime_capability.js");
const {
  classifyRendererRequest,
  createWindowsAclVerifier,
  discoverPrivateRuntime,
  fetchRendererResponse,
  startOrProbePrivateObserver,
} = require("../gateway_proxy.js");

const GENERIC_ERROR = "desktop runtime capability smoke failed";
const MAX_COMPARE_DEPTH = 20;
const MAX_COMPARE_NODES = 8192;
const MAX_POSIX_UNIX_SOCKET_PATH_BYTES = 103;
const ENVIRONMENT_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA"];
const GATEWAY_MODES = new Set(["observe", "advise", "gateway", "unknown"]);
const OPERATIONAL_VALUES = new Set([
  "disabled",
  "starting",
  "ready",
  "degraded",
  "unavailable",
  "unknown",
]);
const SAFE_FAILURE_STAGES = new Set([
  "resolve_collector",
  "create_runtime_root",
  "isolate_environment",
  "discover_runtime",
  "observer_exited",
  "observer_token_missing",
  "observer_token_acl",
  "observer_health_unavailable",
  "fetch_capability",
  "validate_capability",
  "terminate_observer",
  "restore_environment",
  "remove_runtime_root",
  "unknown",
]);

function smokeFailure(stage = null) {
  const error = new Error(GENERIC_ERROR);
  if (SAFE_FAILURE_STAGES.has(stage)) {
    Object.defineProperty(error, "stage", { value: stage });
  }
  return error;
}

function ownDataValue(value, key) {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  if (
    descriptor === undefined ||
    descriptor.enumerable !== true ||
    !Object.prototype.hasOwnProperty.call(descriptor, "value")
  ) {
    return null;
  }
  return descriptor;
}

function isExactClosedValue(actual, expected, depth = 0, counter = { value: 0 }) {
  counter.value += 1;
  if (counter.value > MAX_COMPARE_NODES || depth > MAX_COMPARE_DEPTH) return false;
  if (actual === null || expected === null || typeof actual !== "object") {
    return Object.is(actual, expected);
  }
  if (typeof expected !== "object") return false;

  const actualIsArray = Array.isArray(actual);
  if (actualIsArray !== Array.isArray(expected)) return false;
  if (actualIsArray && actual.length !== expected.length) return false;

  const actualKeys = Reflect.ownKeys(actual).filter(
    (key) => !actualIsArray || key !== "length",
  );
  const expectedKeys = Reflect.ownKeys(expected).filter(
    (key) => !actualIsArray || key !== "length",
  );
  if (
    actualKeys.length !== expectedKeys.length ||
    actualKeys.some((key) => typeof key !== "string")
  ) {
    return false;
  }
  const expectedKeySet = new Set(expectedKeys);
  for (const key of actualKeys) {
    if (!expectedKeySet.has(key)) return false;
    const actualDescriptor = ownDataValue(actual, key);
    const expectedDescriptor = ownDataValue(expected, key);
    if (
      actualDescriptor === null ||
      expectedDescriptor === null ||
      !isExactClosedValue(
        actualDescriptor.value,
        expectedDescriptor.value,
        depth + 1,
        counter,
      )
    ) {
      return false;
    }
  }
  return true;
}

function forbiddenStrings(values) {
  if (!Array.isArray(values)) return null;
  const result = [];
  for (const value of values) {
    if (typeof value !== "string" || value.length === 0) return null;
    result.push(value);
  }
  return result;
}

export function validateRuntimeCapabilitySmoke(payload, forbiddenValues = []) {
  try {
    const forbidden = forbiddenStrings(forbiddenValues);
    const normalized = normalizeRuntimeCapability(payload);
    if (
      forbidden === null ||
      normalized === null ||
      !isExactClosedValue(payload, normalized) ||
      normalized.observer.operational !== "ready" ||
      !GATEWAY_MODES.has(normalized.gateway.mode) ||
      !OPERATIONAL_VALUES.has(normalized.gateway.operational)
    ) {
      throw smokeFailure();
    }
    const aggregate = {
      schemaVersion: "desktop-runtime-capability-smoke/v1",
      object: "desktop.runtime_capability_smoke",
      ok: true,
      observerOperational: normalized.observer.operational,
      gatewayMode: normalized.gateway.mode,
      gatewayOperational: normalized.gateway.operational,
    };
    const serialized = JSON.stringify(aggregate);
    if (forbidden.some((value) => serialized.includes(value))) {
      throw smokeFailure();
    }
    return aggregate;
  } catch {
    throw smokeFailure();
  }
}

function parseCollectorArgument(argv) {
  if (
    !Array.isArray(argv) ||
    argv.length !== 2 ||
    argv[0] !== "--collector" ||
    typeof argv[1] !== "string" ||
    argv[1].length === 0 ||
    /[\u0000-\u001f\u007f]/u.test(argv[1])
  ) {
    throw smokeFailure();
  }
  return argv[1];
}

function resolveCollectorExecutable(value) {
  try {
    const resolved = path.resolve(value);
    const canonical = fs.realpathSync.native(resolved);
    const metadata = fs.lstatSync(canonical);
    if (
      !path.isAbsolute(canonical) ||
      metadata.isSymbolicLink() ||
      !metadata.isFile() ||
      (process.platform !== "win32" && (metadata.mode & 0o111) === 0)
    ) {
      throw smokeFailure();
    }
    if (process.platform !== "win32") {
      fs.accessSync(canonical, fs.constants.X_OK);
    }
    return canonical;
  } catch {
    throw smokeFailure();
  }
}

function createPrivateRuntimeRoot() {
  let createdRoot = null;
  try {
    const tempParent = fs.realpathSync.native(
      process.platform === "win32" ? os.tmpdir() : "/tmp",
    );
    const created = fs.mkdtempSync(path.join(tempParent, "openusage-smoke-"));
    const initial = fs.lstatSync(created);
    createdRoot = { path: created, device: initial.dev, inode: initial.ino };
    const canonical = fs.realpathSync.native(created);
    fs.chmodSync(canonical, 0o700);
    const metadata = fs.lstatSync(canonical);
    if (
      metadata.isSymbolicLink() ||
      !metadata.isDirectory() ||
      metadata.dev !== initial.dev ||
      metadata.ino !== initial.ino ||
      (process.platform !== "win32" && (metadata.mode & 0o777) !== 0o700) ||
      (typeof process.getuid === "function" && metadata.uid !== process.getuid())
    ) {
      throw smokeFailure();
    }
    return { path: canonical, device: metadata.dev, inode: metadata.ino };
  } catch {
    try {
      removeCreatedRoot(createdRoot);
    } catch {
      // Only the generic boundary error is observable.
    }
    throw smokeFailure();
  }
}

function validateLocalApiSocketPath(runtime) {
  if (process.platform === "win32") return;
  const socketPath = runtime?.localApi?.socketPath;
  if (
    runtime?.localApi?.transport !== "unix" ||
    typeof socketPath !== "string" ||
    !path.posix.isAbsolute(socketPath) ||
    Buffer.byteLength(socketPath, "utf8") > MAX_POSIX_UNIX_SOCKET_PATH_BYTES
  ) {
    throw smokeFailure();
  }
}

function isolateEnvironment(root) {
  const saved = new Map();
  const isolated = {
    HOME: path.join(root, "home"),
    USERPROFILE: path.join(root, "profile"),
    LOCALAPPDATA: path.join(root, "local-app-data"),
  };
  for (const key of ENVIRONMENT_KEYS) {
    saved.set(key, {
      present: Object.prototype.hasOwnProperty.call(process.env, key),
      value: process.env[key],
    });
  }
  const restore = () => {
    for (const key of ENVIRONMENT_KEYS) {
      const previous = saved.get(key);
      if (previous.present) process.env[key] = previous.value;
      else delete process.env[key];
    }
  };
  try {
    for (const key of ENVIRONMENT_KEYS) {
      fs.mkdirSync(isolated[key], { mode: 0o700 });
    }
    for (const key of ENVIRONMENT_KEYS) process.env[key] = isolated[key];
  } catch {
    try {
      restore();
    } catch {
      // The caller still receives only the generic boundary error.
    }
    throw smokeFailure();
  }
  return {
    isolated,
    restore,
  };
}

function childHasExited(child) {
  return (
    !child ||
    (child.exitCode !== null && child.exitCode !== undefined) ||
    (child.signalCode !== null && child.signalCode !== undefined)
  );
}

function waitForChildExit(child, timeoutMs) {
  if (childHasExited(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      child.removeListener("exit", onExit);
      resolve(value);
    };
    const onExit = () => finish(true);
    child.once("exit", onExit);
    timer = setTimeout(() => finish(childHasExited(child)), timeoutMs);
  });
}

async function terminateChild(child) {
  if (childHasExited(child)) return;
  try {
    child.kill("SIGTERM");
  } catch {
    // Continue to the bounded hard-stop path.
  }
  if (await waitForChildExit(child, 1_500)) return;
  try {
    child.kill("SIGKILL");
  } catch {
    // Cleanup remains bounded and reports only the generic boundary error.
  }
  if (!(await waitForChildExit(child, 1_000))) throw smokeFailure();
}

function removeCreatedRoot(createdRoot) {
  if (createdRoot === null) return;
  const metadata = fs.lstatSync(createdRoot.path);
  if (
    metadata.isSymbolicLink() ||
    !metadata.isDirectory() ||
    metadata.dev !== createdRoot.device ||
    metadata.ino !== createdRoot.inode
  ) {
    throw smokeFailure();
  }
  fs.rmSync(createdRoot.path, { recursive: true, force: false, maxRetries: 2 });
}

async function runCli(argv) {
  let child = null;
  let createdRoot = null;
  let environment = null;
  let aggregate = null;
  let cleanupFailed = false;
  let stage = "resolve_collector";
  let failureStage = null;
  try {
    const collector = resolveCollectorExecutable(parseCollectorArgument(argv));
    stage = "create_runtime_root";
    createdRoot = createPrivateRuntimeRoot();
    stage = "isolate_environment";
    environment = isolateEnvironment(createdRoot.path);
    stage = "discover_runtime";
    const runtime = discoverPrivateRuntime({
      platform: process.platform,
      homeDir: environment.isolated.HOME,
      localAppData: environment.isolated.LOCALAPPDATA,
    });
    validateLocalApiSocketPath(runtime);
    const verifyWindowsAcl =
      process.platform === "win32" ? createWindowsAclVerifier() : undefined;
    const started = await startOrProbePrivateObserver({
      runtime,
      platform: process.platform,
      command: collector,
      verifyWindowsAcl,
      offline: true,
    });
    child = started?.child ?? null;
    if (started?.ready !== true) {
      const tokenPath = runtime?.localApi?.tokenPath;
      if (childHasExited(child)) stage = "observer_exited";
      else if (typeof tokenPath !== "string" || !fs.existsSync(tokenPath)) {
        stage = "observer_token_missing";
      } else if (
        typeof verifyWindowsAcl === "function" &&
        verifyWindowsAcl(tokenPath) !== true
      ) {
        stage = "observer_token_acl";
      } else {
        stage = "observer_health_unavailable";
      }
      throw smokeFailure(stage);
    }

    stage = "fetch_capability";
    const classified = classifyRendererRequest({
      method: "GET",
      target: "/gateway/v1/health",
      headers: {},
    });
    if (classified === null) throw smokeFailure();
    const response = await fetchRendererResponse(classified, {
      runtime,
      platform: process.platform,
      verifyWindowsAcl,
      deadlineMs: 2_000,
    });
    if (response?.statusCode !== 200 || !Buffer.isBuffer(response.body)) {
      throw smokeFailure();
    }
    stage = "validate_capability";
    const payload = JSON.parse(response.body.toString("utf8"));
    const forbidden = [
      createdRoot.path,
      collector,
      runtime?.localApi?.socketPath,
      runtime?.localApi?.tokenPath,
      runtime?.gateway?.tokenPath,
    ].filter((value) => typeof value === "string" && value.length > 0);
    aggregate = validateRuntimeCapabilitySmoke(payload, forbidden);
  } catch {
    failureStage = stage;
    aggregate = null;
  } finally {
    try {
      await terminateChild(child);
    } catch {
      failureStage ??= "terminate_observer";
      cleanupFailed = true;
    }
    try {
      environment?.restore();
    } catch {
      failureStage ??= "restore_environment";
      cleanupFailed = true;
    }
    if (childHasExited(child)) {
      try {
        removeCreatedRoot(createdRoot);
      } catch {
        failureStage ??= "remove_runtime_root";
        cleanupFailed = true;
      }
    } else {
      cleanupFailed = true;
    }
  }
  if (aggregate === null || cleanupFailed) {
    throw smokeFailure(failureStage ?? "unknown");
  }
  process.stdout.write(`${JSON.stringify(aggregate)}\n`);
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null;
if (invokedPath !== null && invokedPath === fileURLToPath(import.meta.url)) {
  runCli(process.argv.slice(2)).catch((error) => {
    const diagnosticStage =
      process.env.OPENUSAGE_SMOKE_STAGE_DIAGNOSTIC === "1" &&
      SAFE_FAILURE_STAGES.has(error?.stage)
        ? ` stage=${error.stage}`
        : "";
    process.stderr.write(`${GENERIC_ERROR}${diagnosticStage}\n`);
    process.exitCode = 1;
  });
}
