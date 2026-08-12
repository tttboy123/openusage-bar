import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const require = createRequire(import.meta.url);

function temporaryRoot(prefix) {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
}

test("packaged Windows and Linux resolve one observe-only service lifecycle plan", () => {
  const { resolveCollectorLifecyclePlan } = require("../collector_runtime.js");
  const windowsCollector =
    "C:\\Users\\tester\\AppData\\Local\\Programs\\UsageHub\\resources\\collector\\openusage-collector.exe";
  const windows = resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "win32",
    resourcesPath:
      "C:\\Users\\tester\\AppData\\Local\\Programs\\UsageHub\\resources",
    homeDir: "C:\\Users\\tester",
    pathExists: (candidate) => candidate === windowsCollector,
  });
  assert.deepEqual(windows, {
    platform: "win32",
    sourceCommand: windowsCollector,
    serviceCommand: windowsCollector,
    installArgv: [
      "service",
      "install",
      "--interval",
      "300",
      "--command",
      windowsCollector,
    ],
    uninstallArgv: ["service", "uninstall"],
    deleteStateArgv: [
      "state",
      "delete",
      "--confirm",
      "DELETE-LOCAL-USAGEHUB-STATE",
      "--format",
      "json",
    ],
  });

  const linuxSource = "/tmp/.mount_usagehub/resources/collector/openusage-collector";
  const linux = resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "linux",
    resourcesPath: "/tmp/.mount_usagehub/resources",
    homeDir: "/home/tester",
    environment: {},
    pathExists: (candidate) => candidate === linuxSource,
  });
  const stable = "/home/tester/.local/share/usagehub/runtime/openusage-collector";
  assert.deepEqual(linux, {
    platform: "linux",
    sourceCommand: linuxSource,
    serviceCommand: stable,
    installArgv: [
      "desktop-service",
      "install",
      "--interval",
      "300",
    ],
    uninstallArgv: ["desktop-service", "uninstall"],
    deleteStateArgv: [
      "state",
      "delete",
      "--confirm",
      "DELETE-LOCAL-USAGEHUB-STATE",
      "--format",
      "json",
    ],
  });
});

test("Linux invokes the packaged managed-service command and waits for readiness", {
  skip: process.platform === "win32",
}, async (context) => {
  const {
    ensurePackagedObserverService,
    resolveCollectorLifecyclePlan,
  } = require("../collector_runtime.js");
  const root = temporaryRoot("usagehub-lifecycle-");
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const resources = path.join(root, "appimage", "resources");
  const source = path.join(resources, "collector", "openusage-collector");
  const home = path.join(root, "home");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, "collector-v1", { mode: 0o700 });
  const plan = resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "linux",
    resourcesPath: resources,
    homeDir: home,
    environment: {},
  });
  assert.notEqual(plan, null);

  const calls = [];
  const spawnProcess = (command, args, options) => {
    calls.push({ command, args, options });
    const child = new EventEmitter();
    child.kill = () => {};
    process.nextTick(() => child.emit("close", 0, null));
    return child;
  };
  let probes = 0;
  const result = await ensurePackagedObserverService({
    plan,
    spawnProcess,
    probe: async () => ++probes >= 3,
    wait: async () => {},
  });

  assert.deepEqual(result, { state: "ready", command: plan.serviceCommand });
  assert.equal(fs.existsSync(plan.serviceCommand), false);
  assert.deepEqual(calls, [{
    command: plan.sourceCommand,
    args: plan.installArgv,
    options: {
      shell: false,
      stdio: "ignore",
      windowsHide: true,
    },
  }]);
  assert.equal(probes, 3);
});

test("Linux stable probe path honors XDG data without Desktop filesystem mutation", {
  skip: process.platform === "win32",
}, async (context) => {
  const {
    ensurePackagedObserverService,
    resolveCollectorLifecyclePlan,
  } = require("../collector_runtime.js");
  const root = temporaryRoot("usagehub-lifecycle-update-");
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const resources = path.join(root, "resources");
  const source = path.join(resources, "collector", "openusage-collector");
  const dataHome = path.join(root, "xdg-data");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, "collector", { mode: 0o700 });
  const plan = resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "linux",
    resourcesPath: resources,
    homeDir: path.join(root, "home"),
    environment: { XDG_DATA_HOME: dataHome },
  });
  assert.equal(
    plan.serviceCommand,
    path.join(dataHome, "usagehub", "runtime", "openusage-collector"),
  );
  let serviceInstalls = 0;
  const spawnProcess = () => {
    serviceInstalls += 1;
    const child = new EventEmitter();
    child.kill = () => {};
    process.nextTick(() => child.emit("close", 0, null));
    return child;
  };
  assert.equal((await ensurePackagedObserverService({
    plan,
    spawnProcess,
    probe: async () => true,
  })).state, "ready");
  assert.equal(serviceInstalls, 0);
  assert.equal(fs.existsSync(dataHome), false);
  assert.equal(fs.readFileSync(source, "utf8"), "collector");
});

test("Linux headless uninstall delegates managed removal and deletes state only on confirmation", {
  skip: process.platform === "win32",
}, async (context) => {
  const {
    ensurePackagedObserverService,
    parsePackagedLifecycleCommand,
    removePackagedObserverService,
    resolveCollectorLifecyclePlan,
  } = require("../collector_runtime.js");
  assert.deepEqual(
    parsePackagedLifecycleCommand({
      isPackaged: true,
      platform: "linux",
      argv: ["--usagehub-uninstall"],
    }),
    { action: "uninstall", deleteData: false },
  );
  assert.deepEqual(
    parsePackagedLifecycleCommand({
      isPackaged: true,
      platform: "linux",
      argv: ["--usagehub-uninstall", "--delete-data"],
    }),
    { action: "uninstall", deleteData: true },
  );
  assert.deepEqual(
    parsePackagedLifecycleCommand({
      isPackaged: true,
      platform: "linux",
      argv: ["--no-sandbox", "--usagehub-uninstall"],
    }),
    { action: "uninstall", deleteData: false },
  );
  assert.deepEqual(
    parsePackagedLifecycleCommand({
      isPackaged: true,
      platform: "linux",
      argv: ["--no-sandbox", "--usagehub-uninstall", "--delete-data"],
    }),
    { action: "uninstall", deleteData: true },
  );
  for (const argv of [
    ["--delete-data"],
    ["--usagehub-uninstall", "--unknown"],
    ["--usagehub-uninstall", "--delete-data", "extra"],
    ["--usagehub-uninstall", "--no-sandbox"],
    ["--no-sandbox", "--no-sandbox", "--usagehub-uninstall"],
    ["--disable-gpu", "--usagehub-uninstall"],
  ]) {
    assert.deepEqual(
      parsePackagedLifecycleCommand({
        isPackaged: true,
        platform: "linux",
        argv,
      }),
      argv.includes("--usagehub-uninstall") ? { action: "invalid" } : null,
    );
  }

  const root = temporaryRoot("usagehub-lifecycle-remove-");
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const resources = path.join(root, "resources");
  const source = path.join(resources, "collector", "openusage-collector");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, "collector-v1", { mode: 0o700 });
  const plan = resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "linux",
    resourcesPath: resources,
    homeDir: path.join(root, "home"),
    environment: {},
  });
  const calls = [];
  const spawnProcess = (command, args, options) => {
    calls.push({ command, args, options });
    const child = new EventEmitter();
    child.kill = () => {};
    process.nextTick(() => child.emit("close", 0, null));
    return child;
  };
  fs.mkdirSync(path.dirname(plan.serviceCommand), { recursive: true });
  fs.writeFileSync(plan.serviceCommand, "python-owned", { mode: 0o700 });
  calls.length = 0;
  assert.deepEqual(
    await removePackagedObserverService({ plan, spawnProcess }),
    { state: "removed", data: "preserved" },
  );
  assert.equal(fs.readFileSync(plan.serviceCommand, "utf8"), "python-owned");
  assert.deepEqual(calls.map(({ command, args }) => ({ command, args })), [{
    command: plan.sourceCommand,
    args: plan.uninstallArgv,
  }]);

  calls.length = 0;
  assert.deepEqual(
    await removePackagedObserverService({
      plan,
      deleteData: true,
      spawnProcess,
    }),
    { state: "removed", data: "deleted" },
  );
  assert.equal(fs.readFileSync(plan.serviceCommand, "utf8"), "python-owned");
  assert.deepEqual(calls.map(({ command, args }) => ({ command, args })), [
    { command: plan.sourceCommand, args: plan.uninstallArgv },
    { command: plan.sourceCommand, args: plan.deleteStateArgv },
  ]);
});

test("packaged service lifecycle fails closed and delegates stable files to Python", () => {
  const { resolveCollectorLifecyclePlan } = require("../collector_runtime.js");
  assert.equal(resolveCollectorLifecyclePlan({
    isPackaged: false,
    platform: "linux",
    resourcesPath: "/app/resources",
    homeDir: "/home/tester",
    pathExists: () => true,
  }), null);
  assert.equal(resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "darwin",
    resourcesPath: "/Applications/UsageHub.app/Contents/Resources",
    homeDir: "/Users/tester",
    pathExists: () => true,
  }), null);
  assert.equal(resolveCollectorLifecyclePlan({
    isPackaged: true,
    platform: "linux",
    resourcesPath: "/app/resources",
    homeDir: "/home/tester",
    environment: { XDG_DATA_HOME: "relative/private" },
    pathExists: () => true,
  }), null);

  const source = fs.readFileSync(
    new URL("../collector_runtime.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /\b(?:install|remove)StableCollector\b/u);
  assert.doesNotMatch(
    source,
    /fs\.(?:chmod|close|fchmod|fstat|fsync|lstat|mkdir|open|read|rename|rmdir|stat|unlink|write)(?:Sync)?\b/u,
  );
  assert.doesNotMatch(source, /require\("crypto"\)/u);
});

test("service execution rejects forged plans and kills a non-closing child at the bound", async (context) => {
  const {
    ensurePackagedObserverService,
    resolveCollectorLifecyclePlan,
  } = require("../collector_runtime.js");
  let plan;
  if (process.platform === "win32") {
    const resources = "C:\\Program Files\\UsageHub\\resources";
    const source = path.win32.join(
      resources,
      "collector",
      "openusage-collector.exe",
    );
    plan = resolveCollectorLifecyclePlan({
      isPackaged: true,
      platform: "win32",
      resourcesPath: resources,
      pathExists: (candidate) => candidate === source,
    });
  } else {
    const root = temporaryRoot("usagehub-lifecycle-bound-");
    context.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const resources = path.join(root, "resources");
    const source = path.join(resources, "collector", "openusage-collector");
    fs.mkdirSync(path.dirname(source), { recursive: true });
    fs.writeFileSync(source, "collector", { mode: 0o700 });
    plan = resolveCollectorLifecyclePlan({
      isPackaged: true,
      platform: "linux",
      resourcesPath: resources,
      homeDir: path.join(root, "home"),
      environment: {},
    });
  }
  assert.notEqual(plan, null);
  let spawnCount = 0;
  assert.deepEqual(await ensurePackagedObserverService({
    plan: { ...plan, installArgv: ["gateway", "start"] },
    spawnProcess: () => {
      spawnCount += 1;
      throw new Error("must not spawn");
    },
    probe: async () => true,
  }), { state: "unavailable", command: null });
  assert.equal(spawnCount, 0);

  let killed = 0;
  let probes = 0;
  const result = await ensurePackagedObserverService({
    plan,
    commandTimeoutMs: 100,
    spawnProcess: () => {
      spawnCount += 1;
      const child = new EventEmitter();
      child.kill = () => { killed += 1; };
      return child;
    },
    probe: async () => { probes += 1; return false; },
  });
  assert.deepEqual(result, { state: "unavailable", command: null });
  assert.equal(killed, 1);
  assert.equal(probes, 1);
});

test("desktop main handles packaged lifecycle before readiness and keeps service ownership private", () => {
  const source = fs.readFileSync(new URL("../main.js", import.meta.url), "utf8");
  for (const imported of [
    "ensurePackagedObserverService",
    "parsePackagedLifecycleCommand",
    "removePackagedObserverService",
    "resolveCollectorLifecyclePlan",
    "probePrivateObserver",
  ]) {
    assert.match(source, new RegExp(`\\b${imported}\\b`, "u"));
  }
  assert.match(
    source,
    /async function startUsageHub[\s\S]*await runPackagedLifecycleCommand\(\)[\s\S]*app\.whenReady\(\)\.then/u,
  );
  assert.match(
    source,
    /request\.action !== "uninstall"[\s\S]*app\.exit\(2\)/u,
  );
  assert.match(
    source,
    /plan === null[\s\S]*app\.exit\(3\)/u,
  );
  assert.match(
    source,
    /result\.state === "removed" \? 0 : 4/u,
  );
  assert.match(
    source,
    /ensurePackagedObserverService\([\s\S]*probe:\s*\(\)\s*=>\s*probePrivateObserver/u,
  );
  const ensureBody = source.split("async function ensurePrivateObserver()", 2)[1]
    .split("function showObserverUnavailable()", 1)[0];
  assert.ok(
    ensureBody.indexOf("isPackagedObserverServicePlatform()") >= 0 &&
      ensureBody.indexOf("isPackagedObserverServicePlatform()") <
        ensureBody.indexOf("const command = dashboardCommand()"),
    "a packaged Windows/Linux plan failure must stop before transient fallback",
  );
  assert.doesNotMatch(source, /webContents[\s\S]{0,200}usagehub-uninstall/u);
});

test("NSIS removes the service before files and always preserves Windows state", () => {
  const packageJson = require("../package.json");
  assert.deepEqual(packageJson.build.nsis, {
    include: "build/installer.nsh",
    deleteAppDataOnUninstall: false,
  });
  const source = fs.readFileSync(
    new URL("../build/installer.nsh", import.meta.url),
    "utf8",
  );
  assert.match(source, /!macro customUnInstall/u);
  assert.doesNotMatch(source, /--delete-app-data/u);
  assert.match(
    source,
    /openusage-collector\.exe" service uninstall/u,
  );
  assert.doesNotMatch(source, /\bstate delete\b/u);
  assert.doesNotMatch(source, /UsageHubDeleteLocalState/u);
  assert.doesNotMatch(source, /service uninstall --delete-data/u);
  assert.doesNotMatch(source, /RMDir\s+\/r\s+.*openusage-bar/iu);
  const missingLabel = source.indexOf("usagehub_collector_missing:");
  const doneLabel = source.indexOf("usagehub_lifecycle_done:");
  assert.ok(missingLabel >= 0 && doneLabel > missingLabel);
  assert.match(source.slice(missingLabel, doneLabel), /Abort/u);
  const readme = fs.readFileSync(new URL("../README.md", import.meta.url), "utf8");
  assert.match(
    readme,
    /Windows[\s\S]{0,300}always preserves local usage state[\s\S]{0,200}silent uninstall[\s\S]{0,100}updates/iu,
  );
  assert.doesNotMatch(readme, /--delete-app-data/u);
});
