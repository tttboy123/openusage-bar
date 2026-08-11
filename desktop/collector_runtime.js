"use strict";

const fs = require("fs");
const childProcess = require("child_process");
const path = require("path");

const MAX_RUNTIME_PATH_LENGTH = 4096;
const COLLECTOR_INTERVAL_SECONDS = "300";
const DELETE_STATE_CONFIRMATION = "DELETE-LOCAL-USAGEHUB-STATE";
const VALID_LIFECYCLE_PLAN = Symbol("validCollectorLifecyclePlan");

function resolveCollectorCommand({
  isPackaged = false,
  resourcesPath,
  platform = process.platform,
  environment = process.env,
  pathExists = fs.existsSync,
  developmentCandidates = [],
} = {}) {
  try {
    const pathApi = platformPath(platform);
    if (pathApi === null || typeof pathExists !== "function") return null;

    if (isPackaged === true) {
      const root = validatedAbsolutePath(resourcesPath, pathApi);
      if (root === null) return null;
      const executableName =
        platform === "win32" ? "openusage-collector.exe" : "openusage-collector";
      const candidate = pathApi.join(root, "collector", executableName);
      return pathExists(candidate) === true ? candidate : null;
    }

    const candidates = [];
    const override = environment?.USAGEHUB_COLLECTOR;
    if (override !== undefined) candidates.push(override);
    if (Array.isArray(developmentCandidates)) {
      candidates.push(...developmentCandidates);
    }
    for (const value of candidates) {
      const candidate = validatedAbsolutePath(value, pathApi);
      if (candidate !== null && pathExists(candidate) === true) {
        return candidate;
      }
    }
  } catch {
    // Runtime discovery is a fail-closed boundary. Do not reflect a hostile
    // environment value or filesystem error into the renderer.
  }
  return null;
}

function resolveCollectorLifecyclePlan({
  isPackaged = false,
  resourcesPath,
  platform = process.platform,
  homeDir,
  environment = process.env,
  pathExists = fs.existsSync,
} = {}) {
  try {
    if (isPackaged !== true || (platform !== "win32" && platform !== "linux")) {
      return null;
    }
    const sourceCommand = resolveCollectorCommand({
      isPackaged,
      resourcesPath,
      platform,
      environment,
      pathExists,
    });
    if (sourceCommand === null) return null;

    let serviceCommand = sourceCommand;
    if (platform === "linux") {
      const dataRoot = resolveLinuxDataRoot({ homeDir, environment });
      if (dataRoot === null) return null;
      serviceCommand = path.posix.join(
        dataRoot,
        "usagehub",
        "runtime",
        "openusage-collector",
      );
    }
    const plan = {
      platform,
      sourceCommand,
      serviceCommand,
      installArgv: Object.freeze(
        platform === "linux"
          ? [
              "desktop-service",
              "install",
              "--interval",
              COLLECTOR_INTERVAL_SECONDS,
            ]
          : [
              "service",
              "install",
              "--interval",
              COLLECTOR_INTERVAL_SECONDS,
              "--command",
              serviceCommand,
            ],
      ),
      uninstallArgv: Object.freeze(
        platform === "linux"
          ? ["desktop-service", "uninstall"]
          : ["service", "uninstall"],
      ),
      deleteStateArgv: Object.freeze([
        "state",
        "delete",
        "--confirm",
        DELETE_STATE_CONFIRMATION,
        "--format",
        "json",
      ]),
    };
    Object.defineProperty(plan, VALID_LIFECYCLE_PLAN, { value: true });
    return Object.freeze(plan);
  } catch {
    return null;
  }
}

function resolveLinuxDataRoot({ homeDir, environment }) {
  const configured = environment?.XDG_DATA_HOME;
  if (typeof configured === "string" && configured.length > 0) {
    return validatedAbsolutePath(configured, path.posix);
  }
  const home = validatedAbsolutePath(homeDir, path.posix);
  return home === null ? null : path.posix.join(home, ".local", "share");
}

async function ensurePackagedObserverService({
  plan,
  probe,
  spawnProcess = childProcess.spawn,
  wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  attempts = 40,
  commandTimeoutMs = 15_000,
} = {}) {
  if (!validLifecyclePlan(plan) || typeof probe !== "function") {
    return unavailableLifecycleResult();
  }
  try {
    if ((await probe()) === true) {
      return { state: "ready", command: plan.serviceCommand };
    }
    const installed = await runCollectorCommand(
      plan.sourceCommand,
      plan.installArgv,
      { spawnProcess, timeoutMs: commandTimeoutMs },
    );
    if (!installed) return unavailableLifecycleResult();

    const boundedAttempts =
      Number.isInteger(attempts) && attempts >= 1 && attempts <= 40 ? attempts : 40;
    for (let attempt = 0; attempt < boundedAttempts; attempt += 1) {
      if ((await probe()) === true) {
        return { state: "ready", command: plan.serviceCommand };
      }
      if (attempt + 1 < boundedAttempts) await wait(250);
    }
  } catch {
    // Lifecycle operations intentionally collapse every filesystem, process,
    // and private endpoint failure into one renderer-safe unavailable state.
  }
  return unavailableLifecycleResult();
}

function parsePackagedLifecycleCommand({
  isPackaged = false,
  platform = process.platform,
  argv = [],
} = {}) {
  if (!Array.isArray(argv) || !argv.includes("--usagehub-uninstall")) {
    return null;
  }
  if (isPackaged !== true || platform !== "linux") {
    return { action: "invalid" };
  }
  if (argv.length === 1 && argv[0] === "--usagehub-uninstall") {
    return { action: "uninstall", deleteData: false };
  }
  if (
    argv.length === 2 &&
    argv[0] === "--usagehub-uninstall" &&
    argv[1] === "--delete-data"
  ) {
    return { action: "uninstall", deleteData: true };
  }
  return { action: "invalid" };
}

async function removePackagedObserverService({
  plan,
  deleteData = false,
  spawnProcess = childProcess.spawn,
} = {}) {
  if (!validLifecyclePlan(plan) || typeof deleteData !== "boolean") {
    return unavailableLifecycleResult();
  }
  try {
    const removedService = await runCollectorCommand(
      plan.sourceCommand,
      plan.uninstallArgv,
      { spawnProcess },
    );
    if (!removedService) return unavailableLifecycleResult();
    if (deleteData) {
      const deletedState = await runCollectorCommand(
        plan.sourceCommand,
        plan.deleteStateArgv,
        { spawnProcess },
      );
      if (!deletedState) return unavailableLifecycleResult();
    }
    return {
      state: "removed",
      data: deleteData ? "deleted" : "preserved",
    };
  } catch {
    return unavailableLifecycleResult();
  }
}

function runCollectorCommand(
  command,
  args,
  { spawnProcess = childProcess.spawn, timeoutMs = 15_000 } = {},
) {
  if (
    typeof command !== "string" ||
    !path.isAbsolute(command) ||
    !Array.isArray(args) ||
    args.some((argument) => typeof argument !== "string") ||
    typeof spawnProcess !== "function"
  ) {
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    let child;
    let settled = false;
    let timeout;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(value);
    };
    try {
      child = spawnProcess(command, args, {
        shell: false,
        stdio: "ignore",
        windowsHide: true,
      });
      if (!child || typeof child.once !== "function") {
        finish(false);
        return;
      }
      child.once("error", () => finish(false));
      child.once("close", (code, signal) =>
        finish(code === 0 && (signal === null || signal === undefined)),
      );
      const boundedTimeout =
        Number.isInteger(timeoutMs) && timeoutMs >= 100 && timeoutMs <= 30_000
          ? timeoutMs
          : 15_000;
      if (!settled) {
        timeout = setTimeout(() => {
          try {
            child.kill();
          } catch {
            // The closed result below remains authoritative.
          }
          finish(false);
        }, boundedTimeout);
      }
    } catch {
      finish(false);
    }
  });
}

function validLifecyclePlan(plan) {
  if (
    plan === null ||
    typeof plan !== "object" ||
    plan[VALID_LIFECYCLE_PLAN] !== true ||
    !Object.isFrozen(plan) ||
    !sameStringArray(plan.deleteStateArgv, [
      "state",
      "delete",
      "--confirm",
      DELETE_STATE_CONFIRMATION,
      "--format",
      "json",
    ])
  ) {
    return false;
  }
  const pathApi = platformPath(plan.platform);
  if (pathApi === null) return false;
  const source = validatedAbsolutePath(plan.sourceCommand, pathApi);
  const service = validatedAbsolutePath(plan.serviceCommand, pathApi);
  if (source === null || service === null) return false;
  if (plan.platform === "win32") {
    return (
      source === service &&
      sameStringArray(plan.installArgv, [
        "service",
        "install",
        "--interval",
        COLLECTOR_INTERVAL_SECONDS,
        "--command",
        service,
      ]) &&
      sameStringArray(plan.uninstallArgv, ["service", "uninstall"])
    );
  }
  return (
    plan.platform === "linux" &&
    source !== service &&
    sameStringArray(plan.installArgv, [
      "desktop-service",
      "install",
      "--interval",
      COLLECTOR_INTERVAL_SECONDS,
    ]) &&
    sameStringArray(plan.uninstallArgv, ["desktop-service", "uninstall"])
  );
}

function sameStringArray(actual, expected) {
  return (
    Array.isArray(actual) &&
    Object.isFrozen(actual) &&
    actual.length === expected.length &&
    actual.every((value, index) => value === expected[index])
  );
}

function unavailableLifecycleResult() {
  return { state: "unavailable", command: null };
}

function platformPath(platform) {
  if (platform === "win32") return path.win32;
  if (platform === "darwin" || platform === "linux") return path.posix;
  return null;
}

function validatedAbsolutePath(value, pathApi) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_RUNTIME_PATH_LENGTH ||
    /[\u0000-\u001f\u007f]/u.test(value) ||
    !pathApi.isAbsolute(value) ||
    value.split(/[\\/]/u).includes("..")
  ) {
    return null;
  }
  const normalized = pathApi.normalize(value);
  return normalized && pathApi.isAbsolute(normalized) ? normalized : null;
}

module.exports = {
  ensurePackagedObserverService,
  parsePackagedLifecycleCommand,
  removePackagedObserverService,
  resolveCollectorCommand,
  resolveCollectorLifecyclePlan,
};
