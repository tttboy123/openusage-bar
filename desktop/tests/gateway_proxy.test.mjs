import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const require = createRequire(import.meta.url);
const approveFixtureWindowsAcl = () => true;

test("discovers the private Local API and Gateway endpoints per platform", () => {
  const { discoverPrivateRuntime } = require("../gateway_proxy.js");

  const darwin = discoverPrivateRuntime({
    platform: "darwin",
    homeDir: "/Users/example",
  });
  const linux = discoverPrivateRuntime({
    platform: "linux",
    homeDir: "/home/example",
  });
  const windows = discoverPrivateRuntime({
    platform: "win32",
    homeDir: "C:\\Users\\example",
    localAppData: "C:\\Users\\example\\AppData\\Local",
  });

  assert.deepEqual(darwin, {
    localApi: {
      transport: "unix",
      socketPath: "/Users/example/.local/state/openusage-bar/openusage.sock",
    },
    gateway: {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17823,
      tokenPath: "/Users/example/.local/state/openusage-bar/gateway.token",
    },
  });
  assert.deepEqual(linux, {
    localApi: {
      transport: "unix",
      socketPath: "/home/example/.local/state/openusage-bar/openusage.sock",
    },
    gateway: {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17823,
      tokenPath: "/home/example/.local/state/openusage-bar/gateway.token",
    },
  });
  assert.deepEqual(windows, {
    localApi: {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17821,
      tokenPath: "C:\\Users\\example\\AppData\\Local\\openusage-bar\\api.token",
    },
    gateway: {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17823,
      tokenPath: "C:\\Users\\example\\AppData\\Local\\openusage-bar\\gateway.token",
    },
  });
  assert.notEqual(windows.localApi.tokenPath, windows.gateway.tokenPath);
});

test("rejects unsupported or non-private runtime roots without disclosing them", () => {
  const { discoverPrivateRuntime } = require("../gateway_proxy.js");
  const privateCanary = "/private/user/CANARY_RUNTIME_ROOT";
  const cases = [
    { platform: "plan9", homeDir: privateCanary },
    { platform: "darwin", homeDir: "relative/home" },
    { platform: "linux", homeDir: "/home/../shared" },
    { platform: "linux", homeDir: "/home/user\nsecret" },
    { platform: "win32", homeDir: "C:\\Users\\x" },
    { platform: "win32", localAppData: "relative\\state" },
    { platform: "win32", localAppData: "\\\\server\\share\\state" },
  ];
  for (const input of cases) {
    assert.throws(
      () => discoverPrivateRuntime(input),
      (error) =>
        error instanceof Error &&
        error.message === "private runtime unavailable" &&
        !error.message.includes(privateCanary),
      JSON.stringify(input),
    );
  }
});

test("allows only credential-free renderer reads and builds closed upstream headers", () => {
  const {
    RUNTIME_CAPABILITY_MEDIA_TYPE,
    classifyRendererRequest,
  } = require("../gateway_proxy.js");

  assert.deepEqual(
    classifyRendererRequest({
      method: "GET",
      target: "/v1/snapshot?today=2026-08-09",
      headers: {
        accept: "text/html,application/xhtml+xml",
        "if-none-match": 'W/"safe-tag"',
      },
    }),
    {
      service: "localApi",
      method: "GET",
      target: "/v1/snapshot?today=2026-08-09",
      headers: {
        Accept: "application/json",
        "If-None-Match": 'W/"safe-tag"',
      },
    },
  );
  assert.deepEqual(
    classifyRendererRequest({
      method: "GET",
      target: "/gateway/v1/health",
      headers: { accept: "text/html" },
    }),
    {
      service: "gateway",
      method: "GET",
      target: "/gateway/v1/health",
      headers: { Accept: RUNTIME_CAPABILITY_MEDIA_TYPE },
    },
  );
  assert.equal(
    classifyRendererRequest({
      method: "GET",
      target: "/v1/health",
      headers: { authorization: "Bearer renderer-secret" },
    }),
    null,
  );

  for (const request of [
    { method: "POST", target: "/v1/health" },
    { method: "HEAD", target: "/gateway/v1/health" },
    { method: "GET", target: "http://127.0.0.1:17821/v1/health" },
    { method: "GET", target: "//127.0.0.1/v1/health" },
    { method: "GET", target: "/v1/%68ealth" },
    { method: "GET", target: "/v1/missing" },
    { method: "GET", target: "/gateway/v1/health?verbose=1" },
    { method: "GET", target: "/v1/health#private" },
    { method: "GET", target: "/v1/summary?today=a&today=b" },
  ]) {
    assert.equal(
      classifyRendererRequest({ ...request, headers: {} }),
      null,
      `${request.method} ${request.target}`,
    );
  }
});

test(
  "reads only one owner-private POSIX token file without aliases",
  { skip: process.platform === "win32" },
  (context) => {
    const { readPrivateToken } = require("../gateway_proxy.js");
    const root = fs.realpathSync(
      fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-token-")),
    );
    context.after(() => fs.rmSync(root, { recursive: true, force: true }));
    fs.chmodSync(root, 0o700);
    const token = "t".repeat(48);
    const secure = path.join(root, "secure.token");
    fs.writeFileSync(secure, token, { encoding: "ascii", mode: 0o600 });

    assert.equal(readPrivateToken(secure, { platform: process.platform }), token);

    const permissive = path.join(root, "permissive.token");
    fs.writeFileSync(permissive, token, { encoding: "ascii", mode: 0o600 });
    fs.chmodSync(permissive, 0o644);
    assert.throws(
      () => readPrivateToken(permissive, { platform: process.platform }),
      /^Error: private token unavailable$/,
    );

    const hardlink = path.join(root, "hardlink.token");
    fs.linkSync(secure, hardlink);
    assert.throws(
      () => readPrivateToken(secure, { platform: process.platform }),
      /^Error: private token unavailable$/,
    );
    fs.unlinkSync(hardlink);

    const aliasRoot = `${root}-alias`;
    fs.symlinkSync(root, aliasRoot, "dir");
    context.after(() => fs.rmSync(aliasRoot, { force: true }));
    assert.throws(
      () => readPrivateToken(path.join(aliasRoot, "secure.token"), {
        platform: process.platform,
      }),
      /^Error: private token unavailable$/,
    );

    fs.writeFileSync(secure, `${token}\n`, { encoding: "ascii", mode: 0o600 });
    assert.throws(
      () => readPrivateToken(secure, { platform: process.platform }),
      /^Error: private token unavailable$/,
    );

    fs.writeFileSync(secure, Buffer.alloc(48, 0xe1), { mode: 0o600 });
    assert.throws(
      () => readPrivateToken(secure, { platform: process.platform }),
      /^Error: private token unavailable$/,
    );

    fs.writeFileSync(secure, token, { encoding: "ascii", mode: 0o600 });
    const originalClose = fs.closeSync;
    try {
      fs.closeSync = (descriptor) => {
        originalClose(descriptor);
        throw new Error("close failure canary");
      };
      assert.throws(
        () => readPrivateToken(secure, { platform: process.platform }),
        /^Error: private token unavailable$/,
      );
    } finally {
      fs.closeSync = originalClose;
    }
  },
);

test("fails closed on Windows unless a trusted ACL verifier approves the file", (context) => {
  const { readPrivateToken } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-win-token-")),
  );
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const tokenPath = path.join(root, "api.token");
  const token = "a".repeat(48);
  fs.writeFileSync(tokenPath, token, { encoding: "ascii", mode: 0o600 });

  assert.throws(
    () => readPrivateToken(tokenPath, { platform: "win32" }),
    /^Error: private token unavailable$/,
  );
  assert.equal(
    readPrivateToken(tokenPath, {
      platform: "win32",
      verifyWindowsAcl: () => true,
    }),
    token,
  );
});

test("normalizes Windows native realpaths before accepting a private token parent", (context) => {
  const { readPrivateToken } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-win-canonical-")),
  );
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const tokenPath = path.join(root, "api.token");
  const token = "w".repeat(48);
  fs.writeFileSync(tokenPath, token, { encoding: "ascii", mode: 0o600 });

  const realpathCalls = [];
  const resolveCalls = [];
  assert.equal(
    readPrivateToken(tokenPath, {
      platform: "win32",
      verifyWindowsAcl: () => true,
      realpath: (candidate) => {
        realpathCalls.push(candidate);
        return "\\\\?\\C:\\USERS\\EXAMPLE\\APPDATA\\LOCAL\\OPENUSAGE-BAR";
      },
      resolvePath: (candidate) => {
        resolveCalls.push(candidate);
        return "c:\\users\\example\\appdata\\local\\openusage-bar";
      },
    }),
    token,
  );
  assert.deepEqual(realpathCalls, [root]);
  assert.deepEqual(resolveCalls, [root]);

  for (const unsafeResolved of [
    "\\\\?\\UNC\\server\\share\\openusage-bar",
    "\\\\.\\C:\\openusage-bar",
    "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\openusage-bar",
  ]) {
    assert.throws(
      () =>
        readPrivateToken(tokenPath, {
          platform: "win32",
          verifyWindowsAcl: () => true,
          realpath: () => unsafeResolved,
          resolvePath: () => "C:\\openusage-bar",
        }),
      /^Error: private token unavailable$/,
      unsafeResolved,
    );
  }
});

test("runs a fixed bounded Windows ACL verifier without shell interpolation", () => {
  const { createWindowsAclVerifier } = require("../gateway_proxy.js");
  assert.equal(typeof createWindowsAclVerifier, "function");
  const systemRoot = "C:\\Windows";
  const powershell =
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  const tokenPath = "C:\\Users\\example\\AppData\\Local\\openusage-bar\\api.token";
  const calls = [];
  const existingPaths = [];
  const resolvedPaths = [];
  const trustedLocator = {
    systemRoot,
    processArch: "x64",
    pathExists: (candidate) => {
      existingPaths.push(candidate);
      return candidate === systemRoot || candidate === powershell;
    },
    realpath: (candidate) => {
      resolvedPaths.push(candidate);
      return candidate;
    },
  };
  const verifier = createWindowsAclVerifier({
    ...trustedLocator,
    spawnSync: (executable, args, options) => {
      calls.push({ executable, args, options });
      return { status: 0, signal: null, error: undefined, stdout: "OK", stderr: "" };
    },
  });

  assert.equal(verifier(tokenPath), true);
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.executable, powershell);
  assert.notEqual(path.win32.basename(call.executable), call.executable);
  assert.ok(existingPaths.includes(powershell));
  assert.ok(resolvedPaths.includes(powershell));
  assert.deepEqual(call.args.slice(0, 6), [
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
  ]);
  const fixedScript = call.args.at(-1);
  assert.equal(typeof fixedScript, "string");
  assert.equal(call.args.includes(tokenPath), false);
  assert.doesNotMatch(fixedScript, /C:\\Users\\example|api\.token/);
  assert.match(fixedScript, /In\.ReadToEnd/);
  assert.match(fixedScript, /GetOwner/);
  assert.match(fixedScript, /AreAccessRulesProtected/);
  assert.match(fixedScript, /GetAccessRules\(\$true,\s*\$true,/);
  assert.match(fixedScript, /\.Count\s+-ne\s+2/);
  assert.match(fixedScript, /identity\.User(?:\.Value)?/i);
  assert.match(fixedScript, /S-1-5-18/);
  assert.match(fixedScript, /IsInherited/);
  assert.match(
    fixedScript,
    /AccessControlType\s+-ne\s+\[[^\]]+\]::Allow/,
  );
  assert.match(
    fixedScript,
    /FileSystemRights\s+-ne\s+\[[^\]]+\]::FullControl/,
  );
  assert.doesNotMatch(fixedScript, /S-1-1-0|S-1-5-11|S-1-5-32-545/);
  assert.deepEqual(call.options, {
    encoding: "utf8",
    input: tokenPath,
    maxBuffer: 8 * 1024,
    shell: false,
    timeout: 2_000,
    windowsHide: true,
  });

  for (const result of [
    { status: 1, signal: null, stdout: "OK", stderr: "" },
    { status: 0, signal: "SIGTERM", stdout: "OK", stderr: "" },
    { status: 0, signal: null, stdout: "DENY", stderr: "" },
    { status: 0, signal: null, stdout: "OK\nprivate-path", stderr: "" },
    { status: 0, signal: null, stdout: "OK", stderr: "private-error" },
    { status: null, signal: null, error: new Error("spawn failure"), stdout: "" },
  ]) {
    const denied = createWindowsAclVerifier({
      ...trustedLocator,
      spawnSync: () => result,
    });
    assert.equal(denied(tokenPath), false);
  }
  assert.equal(verifier("relative\\api.token"), false);
  assert.equal(verifier("\\\\server\\share\\api.token"), false);
  assert.equal(verifier("C:\\state\\api.token\nsecret"), false);
});

test("accepts local extended Windows realpaths but rejects device and UNC forms", () => {
  const { createWindowsAclVerifier } = require("../gateway_proxy.js");
  const systemRoot = "C:\\Windows";
  const powershell =
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  const tokenPath = "C:\\Users\\example\\AppData\\Local\\openusage-bar\\api.token";
  const calls = [];
  const verifier = createWindowsAclVerifier({
    systemRoot,
    processArch: "x64",
    pathExists: (candidate) =>
      candidate === systemRoot || candidate === powershell,
    realpath: (candidate) => `\\\\?\\${candidate.toLowerCase()}`,
    spawnSync: (executable) => {
      calls.push(executable);
      return { status: 0, signal: null, error: undefined, stdout: "OK", stderr: "" };
    },
  });
  assert.equal(verifier(tokenPath), true);
  assert.deepEqual(calls, [powershell]);

  for (const unsafeResolved of [
    "\\\\?\\UNC\\server\\share\\Windows",
    "\\\\.\\C:\\Windows",
    "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\Windows",
  ]) {
    let spawned = false;
    const denied = createWindowsAclVerifier({
      systemRoot,
      processArch: "x64",
      pathExists: () => true,
      realpath: (candidate) =>
        candidate === systemRoot ? unsafeResolved : candidate,
      spawnSync: () => {
        spawned = true;
        return { status: 0, signal: null, stdout: "OK", stderr: "" };
      },
    });
    assert.equal(denied(tokenPath), false, unsafeResolved);
    assert.equal(spawned, false, unsafeResolved);
  }
});

test("resolves PowerShell only from a canonical local SystemRoot", () => {
  const { createWindowsAclVerifier } = require("../gateway_proxy.js");
  const tokenPath = "C:\\Users\\example\\AppData\\Local\\openusage-bar\\api.token";
  const powershell =
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";

  for (const systemRoot of [
    null,
    "",
    "Windows",
    "\\\\server\\share\\Windows",
    "C:\\Windows\\..\\Windows",
    "C:\\Win\ndows",
  ]) {
    const spawned = [];
    const verifier = createWindowsAclVerifier({
      systemRoot,
      processArch: "x64",
      pathExists: () => true,
      realpath: (candidate) => candidate,
      spawnSync: (...args) => {
        spawned.push(args);
        return { status: 0, signal: null, stdout: "OK", stderr: "" };
      },
    });
    assert.equal(verifier(tokenPath), false, String(systemRoot));
    assert.deepEqual(spawned, [], String(systemRoot));
  }

  for (const locator of [
    {
      pathExists: () => false,
      realpath: (candidate) => candidate,
    },
    {
      pathExists: () => true,
      realpath: (candidate) =>
        candidate === powershell
          ? "C:\\Untrusted\\powershell.exe"
          : candidate,
    },
  ]) {
    const spawned = [];
    const verifier = createWindowsAclVerifier({
      systemRoot: "C:\\Windows",
      processArch: "x64",
      ...locator,
      spawnSync: (...args) => {
        spawned.push(args);
        return { status: 0, signal: null, stdout: "OK", stderr: "" };
      },
    });
    assert.equal(verifier(tokenPath), false);
    assert.deepEqual(spawned, []);
  }
});

test("a 32-bit process prefers canonical Sysnative PowerShell and safely falls back", () => {
  const { createWindowsAclVerifier } = require("../gateway_proxy.js");
  const tokenPath = "C:\\Users\\example\\AppData\\Local\\openusage-bar\\api.token";
  const systemRoot = "C:\\Windows";
  const sysnative =
    "C:\\Windows\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe";
  const system32 =
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";

  for (const [available, expected] of [
    [new Set([systemRoot, sysnative, system32]), sysnative],
    [new Set([systemRoot, system32]), system32],
  ]) {
    const calls = [];
    const verifier = createWindowsAclVerifier({
      systemRoot,
      processArch: "ia32",
      pathExists: (candidate) => available.has(candidate),
      realpath: (candidate) => candidate,
      spawnSync: (executable, args, options) => {
        calls.push({ executable, args, options });
        return { status: 0, signal: null, stdout: "OK", stderr: "" };
      },
    });

    assert.equal(verifier(tokenPath), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].executable, expected);
    assert.equal(calls[0].options.shell, false);
  }
});

test("builds the offline Observer daemon arguments", () => {
  const { observerDaemonArguments } = require("../gateway_proxy.js");
  const runtime = {
    localApi: {
      transport: "unix",
      socketPath: "/Users/example/.local/state/openusage-bar/openusage.sock",
    },
    gateway: null,
  };

  assert.deepEqual(observerDaemonArguments(runtime, { offline: true }), [
    "--offline",
    "daemon",
    "--interval",
    "300",
    "--api-transport",
    "unix",
    "--api-socket",
    "/Users/example/.local/state/openusage-bar/openusage.sock",
  ]);
});

test("starts one bounded private Observer daemon only after a failed probe", async () => {
  const {
    observerDaemonArguments,
    startOrProbePrivateObserver,
  } = require("../gateway_proxy.js");
  assert.equal(typeof observerDaemonArguments, "function");
  assert.equal(typeof startOrProbePrivateObserver, "function");
  const unixRuntime = {
    localApi: {
      transport: "unix",
      socketPath: "/Users/example/.local/state/openusage-bar/openusage.sock",
    },
    gateway: null,
  };
  assert.deepEqual(observerDaemonArguments(unixRuntime), [
    "daemon",
    "--interval",
    "300",
    "--api-transport",
    "unix",
    "--api-socket",
    "/Users/example/.local/state/openusage-bar/openusage.sock",
  ]);
  assert.deepEqual(
    observerDaemonArguments({
      localApi: {
        transport: "tcp",
        host: "127.0.0.1",
        port: 17821,
        tokenPath: "C:\\state\\openusage-bar\\api.token",
      },
      gateway: null,
    }),
    [
      "daemon",
      "--interval",
      "300",
      "--api-transport",
      "tcp",
      "--api-port",
      "17821",
      "--api-token-path",
      "C:\\state\\openusage-bar\\api.token",
    ],
  );

  const probes = [false, false, true];
  const spawnCalls = [];
  const child = new EventEmitter();
  child.kill = () => true;
  const started = await startOrProbePrivateObserver({
    runtime: unixRuntime,
    platform: "darwin",
    command: "/Applications/OpenUsage Bar.app/Contents/MacOS/openusage-bar",
    probeObserver: async () => probes.shift() ?? false,
    spawnProcess: (...args) => {
      spawnCalls.push(args);
      return child;
    },
    wait: async () => {},
    attempts: 4,
  });
  assert.deepEqual(started, { ready: true, child });
  assert.equal(spawnCalls.length, 1);
  assert.deepEqual(spawnCalls[0], [
    "/Applications/OpenUsage Bar.app/Contents/MacOS/openusage-bar",
    observerDaemonArguments(unixRuntime),
    { shell: false, stdio: "ignore", windowsHide: true },
  ]);

  let readySpawned = false;
  const alreadyReady = await startOrProbePrivateObserver({
    runtime: unixRuntime,
    platform: "darwin",
    command: "/Applications/OpenUsage Bar.app/Contents/MacOS/openusage-bar",
    probeObserver: async () => true,
    spawnProcess: () => {
      readySpawned = true;
      return child;
    },
  });
  assert.deepEqual(alreadyReady, { ready: true, child: null });
  assert.equal(readySpawned, false);

  const invalidCommand = await startOrProbePrivateObserver({
    runtime: unixRuntime,
    platform: "darwin",
    command: "relative-openusage-bar",
    probeObserver: async () => false,
    spawnProcess: () => {
      throw new Error("must not spawn");
    },
  });
  assert.deepEqual(invalidCommand, { ready: false, child: null });
});

test("starts the private Observer with offline daemon arguments", async () => {
  const { startOrProbePrivateObserver } = require("../gateway_proxy.js");
  const runtime = {
    localApi: {
      transport: "unix",
      socketPath: "/Users/example/.local/state/openusage-bar/openusage.sock",
    },
    gateway: null,
  };
  const command = "/Applications/OpenUsage Bar.app/Contents/MacOS/openusage-bar";
  const probes = [false, true];
  const spawnCalls = [];
  const child = new EventEmitter();
  child.kill = () => true;

  const result = await startOrProbePrivateObserver({
    runtime,
    platform: "darwin",
    command,
    offline: true,
    probeObserver: async () => probes.shift() ?? false,
    spawnProcess: (...args) => {
      spawnCalls.push(args);
      return child;
    },
    wait: async () => {},
    attempts: 1,
  });

  assert.deepEqual(result, { ready: true, child });
  assert.deepEqual(spawnCalls, [[
    command,
    [
      "--offline",
      "daemon",
      "--interval",
      "300",
      "--api-transport",
      "unix",
      "--api-socket",
      "/Users/example/.local/state/openusage-bar/openusage.sock",
    ],
    { shell: false, stdio: "ignore", windowsHide: true },
  ]]);
});

test("tray reads only fixed Observer GET resources through the main-process token boundary", async (context) => {
  const { fetchPrivateObserverJson } = require("../gateway_proxy.js");
  assert.equal(typeof fetchPrivateObserverJson, "function");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-tray-token-")),
  );
  fs.chmodSync(root, 0o700);
  const privateToken = "p".repeat(48);
  const rendererToken = "RENDERER_TOKEN_MUST_NOT_CROSS";
  const tokenPath = path.join(root, "api.token");
  fs.writeFileSync(tokenPath, privateToken, { encoding: "ascii", mode: 0o600 });

  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push({
      method: request.method,
      url: request.url,
      headers: { ...request.headers },
    });
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.end(
      request.url === "/v1/snapshot"
        ? '{"summary":{"todayTokens":42}}'
        : '{"quotaWindows":[]}',
    );
  });
  const port = await listenTcp(server);
  context.after(async () => {
    await closeServer(server);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const runtime = {
    localApi: {
      transport: "tcp",
      host: "127.0.0.1",
      port,
      tokenPath,
    },
    gateway: null,
  };
  const aclChecks = [];
  const options = {
    runtime,
    platform: "win32",
    verifyWindowsAcl: (candidate) => {
      aclChecks.push(candidate);
      return true;
    },
    method: "POST",
    headers: {
      Authorization: `Bearer ${rendererToken}`,
      Cookie: "renderer-cookie=must-not-cross",
      "If-None-Match": 'W/"renderer-etag"',
      "X-Renderer-Canary": "must-not-cross",
    },
  };

  assert.deepEqual(
    await fetchPrivateObserverJson("/v1/snapshot", options),
    { summary: { todayTokens: 42 } },
  );
  assert.deepEqual(
    await fetchPrivateObserverJson("/v1/capacity", options),
    { quotaWindows: [] },
  );
  assert.deepEqual(
    requests.map(({ method, url }) => ({ method, url })),
    [
      { method: "GET", url: "/v1/snapshot" },
      { method: "GET", url: "/v1/capacity" },
    ],
  );
  for (const request of requests) {
    assert.equal(request.headers.accept, "application/json");
    assert.equal(request.headers.authorization, `Bearer ${privateToken}`);
    assert.equal(request.headers.cookie, undefined);
    assert.equal(request.headers["if-none-match"], undefined);
    assert.equal(request.headers["x-renderer-canary"], undefined);
    assert.doesNotMatch(JSON.stringify(request), /17822|RENDERER_TOKEN/);
  }
  assert.deepEqual(aclChecks, [tokenPath, tokenPath]);

  for (const target of [
    "/v1/health",
    "/v1/snapshot?today=2026-08-09",
    "/v1/capacity?limit=1",
    "/v1/%73napshot",
    "//127.0.0.1/v1/snapshot",
    "http://127.0.0.1:17822/v1/snapshot",
  ]) {
    assert.equal(
      await fetchPrivateObserverJson(target, options),
      null,
      target,
    );
  }
  assert.equal(requests.length, 2);
  assert.deepEqual(aclChecks, [tokenPath, tokenPath]);

  assert.equal(
    await fetchPrivateObserverJson("/v1/snapshot", {
      ...options,
      verifyWindowsAcl: () => false,
    }),
    null,
  );
  assert.equal(requests.length, 2);
});

test("tray Observer failures collapse to null without private diagnostics", async (context) => {
  const { fetchPrivateObserverJson } = require("../gateway_proxy.js");
  assert.equal(typeof fetchPrivateObserverJson, "function");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-tray-failure-")),
  );
  fs.chmodSync(root, 0o700);
  const tokenPath = path.join(root, "CANARY_PRIVATE_API_TOKEN_PATH.token");
  const privateToken = "q".repeat(48);
  fs.writeFileSync(tokenPath, privateToken, { encoding: "ascii", mode: 0o600 });
  let mode = "malformed";
  const server = http.createServer((_request, response) => {
    if (mode === "stall") return;
    if (mode === "oversized") {
      response.end(Buffer.alloc(4 * 1024 * 1024 + 1, 0x61));
      return;
    }
    if (mode === "raw-error") {
      response.statusCode = 500;
      response.end(JSON.stringify({
        token: privateToken,
        path: tokenPath,
        rawError: "CANARY_UPSTREAM_RAW_ERROR",
      }));
      return;
    }
    response.end('{"summary":{"todayTokens":');
  });
  const port = await listenTcp(server);
  context.after(async () => {
    await closeServer(server);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const options = {
    runtime: {
      localApi: {
        transport: "tcp",
        host: "127.0.0.1",
        port,
        tokenPath,
      },
      gateway: null,
    },
    platform: "win32",
    verifyWindowsAcl: () => true,
    deadlineMs: 100,
  };

  const failures = [];
  for (const failureMode of ["malformed", "raw-error", "oversized"]) {
    mode = failureMode;
    failures.push(await fetchPrivateObserverJson("/v1/snapshot", options));
  }
  mode = "stall";
  const started = Date.now();
  failures.push(
    await fetchPrivateObserverJson("/v1/snapshot", {
      ...options,
      deadlineMs: 50,
    }),
  );
  assert.ok(Date.now() - started < 1_000);

  await closeServer(server);
  failures.push(await fetchPrivateObserverJson("/v1/snapshot", options));
  failures.push(
    await fetchPrivateObserverJson("/v1/snapshot", {
      ...options,
      verifyWindowsAcl: () => {
        throw new Error(
          `CANARY_ACL_RAW_ERROR ${privateToken} ${tokenPath}`,
        );
      },
    }),
  );

  assert.deepEqual(failures, [null, null, null, null, null, null]);
  assert.doesNotMatch(
    JSON.stringify(failures),
    /CANARY|api\.token|private service|q{48}|17822/,
  );
});

test(
  "tray Observer reads retain the injected Linux private-socket boundary",
  { skip: process.platform === "win32" },
  async (context) => {
    const { fetchPrivateObserverJson } = require("../gateway_proxy.js");
    assert.equal(typeof fetchPrivateObserverJson, "function");
    const socketTempRoot = process.platform === "darwin" ? "/tmp" : os.tmpdir();
    const root = fs.realpathSync(
      fs.mkdtempSync(path.join(socketTempRoot, "oub-tray-")),
    );
    fs.chmodSync(root, 0o700);
    const socketPath = path.join(root, "openusage.sock");
    const requests = [];
    const server = http.createServer((request, response) => {
      requests.push({
        method: request.method,
        url: request.url,
        headers: { ...request.headers },
      });
      response.end('{"quotaWindows":[]}');
    });
    await listenUnix(server, socketPath);
    fs.chmodSync(socketPath, 0o600);
    context.after(async () => {
      await closeServer(server);
      fs.rmSync(root, { recursive: true, force: true });
    });
    const options = {
      runtime: {
        localApi: { transport: "unix", socketPath },
        gateway: null,
      },
      platform: "linux",
      method: "POST",
      headers: {
        Authorization: "Bearer renderer-must-not-cross",
        Cookie: "renderer-cookie=must-not-cross",
      },
    };

    assert.deepEqual(
      await fetchPrivateObserverJson("/v1/capacity", options),
      { quotaWindows: [] },
    );
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, "GET");
    assert.equal(requests[0].url, "/v1/capacity");
    assert.equal(requests[0].headers.host, "localhost");
    assert.equal(requests[0].headers.accept, "application/json");
    assert.equal(requests[0].headers.authorization, undefined);
    assert.equal(requests[0].headers.cookie, undefined);

    fs.chmodSync(socketPath, 0o666);
    assert.equal(
      await fetchPrivateObserverJson("/v1/capacity", options),
      null,
    );
    assert.equal(requests.length, 1);
  },
);

test("returns null and destroys an oversized legacy tray JSON response", async () => {
  const { fetchLegacyDashboardJson } = require("../gateway_proxy.js");
  assert.equal(typeof fetchLegacyDashboardJson, "function");
  let requestDestroyed = false;
  let responseDestroyed = false;
  const requestFactory = (_url, options, onResponse) => {
    assert.deepEqual(options, { agent: false, method: "GET" });
    const request = new EventEmitter();
    request.destroy = () => {
      requestDestroyed = true;
    };
    request.end = () => {
      queueMicrotask(() => {
        const response = new EventEmitter();
        response.statusCode = 200;
        response.headers = {};
        response.destroy = () => {
          responseDestroyed = true;
        };
        onResponse(response);
        response.emit("data", Buffer.from("12345"));
        response.emit("data", Buffer.from("67890"));
        response.emit("end");
      });
    };
    return request;
  };

  const result = await fetchLegacyDashboardJson(
    "http://127.0.0.1:17822/v1/snapshot",
    { requestFactory, maxBytes: 8, maxChunks: 10, deadlineMs: 100 },
  );
  assert.equal(result, null);
  assert.equal(requestDestroyed, true);
  assert.equal(responseDestroyed, true);
});

test("returns a bounded valid legacy tray JSON response", async (context) => {
  const { fetchLegacyDashboardJson } = require("../gateway_proxy.js");
  const server = http.createServer((_request, response) => {
    response.setHeader("Content-Type", "application/json");
    response.end('{"summary":{"todayTokens":42}}');
  });
  const port = await listenTcp(server);
  context.after(() => closeServer(server));

  assert.deepEqual(
    await fetchLegacyDashboardJson(
      `http://127.0.0.1:${port}/v1/snapshot`,
      { deadlineMs: 100 },
    ),
    { summary: { todayTokens: 42 } },
  );
});

test("returns null and destroys a legacy tray response with too many chunks", async () => {
  const { fetchLegacyDashboardJson } = require("../gateway_proxy.js");
  let requestDestroyed = false;
  let responseDestroyed = false;
  const requestFactory = (_url, _options, onResponse) => {
    const request = new EventEmitter();
    request.destroy = () => {
      requestDestroyed = true;
    };
    request.end = () => {
      queueMicrotask(() => {
        const response = new EventEmitter();
        response.statusCode = 200;
        response.headers = {};
        response.destroy = () => {
          responseDestroyed = true;
        };
        onResponse(response);
        response.emit("data", Buffer.from("{"));
        response.emit("data", Buffer.from('"ok"'));
        response.emit("data", Buffer.from(":true}"));
        response.emit("end");
      });
    };
    return request;
  };

  const result = await fetchLegacyDashboardJson(
    "http://127.0.0.1:17822/v1/capacity",
    { requestFactory, maxBytes: 100, maxChunks: 2, deadlineMs: 100 },
  );
  assert.equal(result, null);
  assert.equal(requestDestroyed, true);
  assert.equal(responseDestroyed, true);
});

test("returns null and destroys a stalled legacy tray response at its deadline", async () => {
  const { fetchLegacyDashboardJson } = require("../gateway_proxy.js");
  let requestDestroyed = false;
  let responseDestroyed = false;
  const requestFactory = (_url, _options, onResponse) => {
    const request = new EventEmitter();
    request.destroy = () => {
      requestDestroyed = true;
    };
    request.end = () => {
      queueMicrotask(() => {
        const response = new EventEmitter();
        response.statusCode = 200;
        response.headers = {};
        response.destroy = () => {
          responseDestroyed = true;
        };
        onResponse(response);
      });
    };
    return request;
  };

  const started = Date.now();
  const result = await fetchLegacyDashboardJson(
    "http://127.0.0.1:17822/v1/snapshot",
    { requestFactory, maxBytes: 100, maxChunks: 10, deadlineMs: 50 },
  );
  assert.equal(result, null);
  assert.ok(Date.now() - started < 500);
  assert.equal(requestDestroyed, true);
  assert.equal(responseDestroyed, true);
});

test("kills and clears a legacy dashboard child when startup never becomes ready", async () => {
  const { startOrProbeLegacyDashboard } = require("../gateway_proxy.js");
  assert.equal(typeof startOrProbeLegacyDashboard, "function");
  const child = new EventEmitter();
  let killCount = 0;
  child.kill = () => {
    killCount += 1;
    return true;
  };
  const spawnCalls = [];

  const result = await startOrProbeLegacyDashboard({
    command: "/opt/openusage-bar/bin/openusage-provider-settings",
    probeDashboard: async () => false,
    spawnProcess: (command, args, options) => {
      spawnCalls.push({ command, args, options });
      return child;
    },
    wait: async () => {},
    attempts: 2,
  });

  assert.deepEqual(result, { ready: false, child: null });
  assert.equal(killCount, 1);
  assert.deepEqual(spawnCalls, [
    {
      command: "/opt/openusage-bar/bin/openusage-provider-settings",
      args: ["dashboard", "--port", "17822"],
      options: { shell: false, stdio: "ignore", windowsHide: true },
    },
  ]);
});

test("keeps a legacy dashboard child only after the readiness probe succeeds", async () => {
  const { startOrProbeLegacyDashboard } = require("../gateway_proxy.js");
  const child = new EventEmitter();
  let killCount = 0;
  child.kill = () => {
    killCount += 1;
    return true;
  };
  const probes = [false, true];
  const result = await startOrProbeLegacyDashboard({
    command: "/opt/openusage-bar/bin/openusage-provider-settings",
    probeDashboard: async () => probes.shift() ?? false,
    spawnProcess: () => child,
    wait: async () => {},
    attempts: 2,
  });

  assert.equal(result.ready, true);
  assert.equal(result.child, child);
  assert.equal(killCount, 0);
});

test("rebuilds only renderer-safe capability facts and isolates malformed Gateway state", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const input = runtimeCapabilityEnvelope();
  input.privatePath = "/private/runtime/openusage.sock";
  input.gateway.bearer = "g".repeat(48);
  input.gateway.features.listener.rawError = "provider stack secret";
  input.gateway.lastError.message = "upstream private message";

  const normalized = normalizeRuntimeCapability(input);
  assert.deepEqual(normalized, safeRuntimeCapabilityEnvelope());
  assert.doesNotMatch(
    JSON.stringify(normalized),
    /private|bearer|provider stack|upstream private|rawError|message/,
  );

  const malformed = runtimeCapabilityEnvelope();
  malformed.gateway.features.listener.operational = "future-ready";
  assert.deepEqual(normalizeRuntimeCapability(malformed), {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: safeObserver(),
    gateway: unknownGateway(),
  });
  assert.equal(normalizeRuntimeCapability({ object: "runtime.private" }), null);
});

test("normalizes missing Observer facts independently like Python and Web", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const fallbacks = {
    operational: "unknown",
    generatedAt: null,
    lastGoodAt: null,
    dataRevision: null,
    schemaVersion: null,
  };

  for (const [field, fallback] of Object.entries(fallbacks)) {
    const input = runtimeCapabilityEnvelope();
    delete input.observer[field];
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(
      normalized.observer,
      { ...safeObserver(), [field]: fallback },
      field,
    );
    assert.deepEqual(normalized.gateway, safeRuntimeCapabilityEnvelope().gateway);
  }
});

test("accepts only own JSON data properties at every capability boundary", () => {
  const {
    composeRendererCapability,
    normalizeRuntimeCapability,
  } = require("../runtime_capability.js");
  const ownNullPrototype = Object.assign(
    Object.create(null),
    runtimeCapabilityEnvelope(),
  );
  assert.deepEqual(
    normalizeRuntimeCapability(ownNullPrototype),
    safeRuntimeCapabilityEnvelope(),
  );

  for (const field of ["apiVersion", "object", "observer", "gateway"]) {
    const source = runtimeCapabilityEnvelope();
    const inherited = Object.create({ [field]: source[field] });
    for (const [key, value] of Object.entries(source)) {
      if (key !== field) inherited[key] = value;
    }
    assert.equal(normalizeRuntimeCapability(inherited), null, field);
  }

  const inheritedGateway = runtimeCapabilityEnvelope();
  inheritedGateway.gateway = Object.create(inheritedGateway.gateway);
  assert.deepEqual(normalizeRuntimeCapability(inheritedGateway).gateway, unknownGateway());

  const inheritedFeature = runtimeCapabilityEnvelope();
  const originalFeatures = inheritedFeature.gateway.features;
  inheritedFeature.gateway.features = Object.assign(
    Object.create({ listener: originalFeatures.listener }),
    Object.fromEntries(
      Object.entries(originalFeatures).filter(([featureId]) => featureId !== "listener"),
    ),
  );
  assert.deepEqual(normalizeRuntimeCapability(inheritedFeature).gateway, unknownGateway());

  const accessor = runtimeCapabilityEnvelope();
  Object.defineProperty(accessor.gateway, "mode", {
    enumerable: true,
    get() {
      throw new Error("CANARY_PRIVATE_GETTER");
    },
  });
  const accessorResult = normalizeRuntimeCapability(accessor);
  assert.deepEqual(accessorResult.observer, safeObserver());
  assert.deepEqual(accessorResult.gateway, unknownGateway());
  assert.doesNotMatch(JSON.stringify(accessorResult), /CANARY_PRIVATE_GETTER/);

  const inheritedError = runtimeCapabilityEnvelope();
  inheritedError.gateway.lastError = new Proxy(
    {
      code: "provider_unavailable",
      retryable: true,
    },
    {
      has(target, key) {
        return key === "observedAt" || Reflect.has(target, key);
      },
      get(target, key, receiver) {
        return key === "observedAt"
          ? "2026-08-09T01:00:00Z"
          : Reflect.get(target, key, receiver);
      },
    },
  );
  assert.deepEqual(normalizeRuntimeCapability(inheritedError).gateway.lastError, {
    code: "capability_invalid",
    retryable: false,
  });

  const inheritedHealth = new Proxy(
    {
      schemaVersion: "1.0",
      dataRevision: 1,
      health: { ok: true, status: "ok" },
    },
    {
      get(target, key, receiver) {
        return key === "generatedAt"
          ? "2026-08-09T01:00:00Z"
          : Reflect.get(target, key, receiver);
      },
    },
  );
  const inheritedHealthResult = composeRendererCapability({
    gatewayPayload: null,
    localHealthPayload: inheritedHealth,
  });
  assert.deepEqual(inheritedHealthResult.observer, {
    operational: "unknown",
    generatedAt: null,
    lastGoodAt: null,
    dataRevision: null,
    schemaVersion: null,
  });
});

test("rejects timestamp year 0000 while preserving year 0001", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const zero = runtimeCapabilityEnvelope();
  zero.observer.generatedAt = "0000-01-01T00:00:00Z";
  zero.observer.lastGoodAt = "0000-12-31T23:59:59Z";
  zero.gateway.lastError.observedAt = "0000-06-01T00:00:00Z";
  const zeroResult = normalizeRuntimeCapability(zero);
  assert.equal(zeroResult.observer.generatedAt, null);
  assert.equal(zeroResult.observer.lastGoodAt, null);
  assert.deepEqual(zeroResult.gateway.lastError, {
    code: "capability_invalid",
    retryable: false,
  });

  const one = runtimeCapabilityEnvelope();
  one.observer.generatedAt = "0001-01-01T00:00:00Z";
  one.observer.lastGoodAt = "0001-12-31T23:59:59Z";
  one.gateway.lastError.observedAt = "0001-06-01T00:00:00Z";
  const oneResult = normalizeRuntimeCapability(one);
  assert.equal(oneResult.observer.generatedAt, one.observer.generatedAt);
  assert.equal(oneResult.observer.lastGoodAt, one.observer.lastGoodAt);
  assert.equal(
    oneResult.gateway.lastError.observedAt,
    one.gateway.lastError.observedAt,
  );
});

test("merges Observer health independently when Gateway is unavailable", () => {
  const { composeRendererCapability } = require("../runtime_capability.js");
  const localHealth = {
    schemaVersion: "1.0",
    dataRevision: 23,
    generatedAt: "2026-08-09T02:03:04Z",
    health: { ok: true, status: "ok" },
    socketPath: "/private/must-not-cross.sock",
  };

  const result = composeRendererCapability({
    gatewayPayload: null,
    localHealthPayload: localHealth,
    gatewayFailure: "gateway_timeout",
  });

  assert.deepEqual(result, {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: {
      operational: "ready",
      generatedAt: "2026-08-09T02:03:04Z",
      lastGoodAt: "2026-08-09T02:03:04Z",
      dataRevision: 23,
      schemaVersion: "1.0",
    },
    gateway: {
      ...unknownGateway(),
      operational: "unavailable",
      lastError: { code: "gateway_timeout", retryable: true },
    },
  });
  assert.doesNotMatch(JSON.stringify(result), /private|socketPath|\.sock/);
});

test("limits schemaVersion to the shared 64-character contract", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const input = runtimeCapabilityEnvelope();
  input.observer.schemaVersion = `openusage/v${"1".repeat(54)}`;
  assert.equal(input.observer.schemaVersion.length, 65);

  const normalized = normalizeRuntimeCapability(input);
  assert.equal(normalized.observer.schemaVersion, null);
});

test("counts bounded astral text as Unicode code points", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const atLimit = crossRuntimeCapabilityEnvelope();
  atLimit.futureText = "\u{1F680}".repeat(256);
  const accepted = normalizeRuntimeCapability(atLimit);
  assert.notEqual(accepted, null);
  assert.equal(accepted.observer.dataRevision, 42);
  assert.equal(JSON.stringify(accepted).includes("\u{1F680}"), false);

  const overLimit = crossRuntimeCapabilityEnvelope();
  overLimit.futureText = "\u{1F680}".repeat(257);
  assert.equal(normalizeRuntimeCapability(overLimit), null);
});

test("rejects lone surrogate text without reflecting it", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  for (const surrogate of ["\uD800", "\uDC00"]) {
    const input = crossRuntimeCapabilityEnvelope();
    input.futureText = surrogate;
    const normalized = normalizeRuntimeCapability(input);
    assert.equal(normalized, null);
    assert.equal(JSON.stringify(normalized).includes(surrogate), false);
  }
});

test("observe mode accepts only an explicitly disabled listener", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const valid = runtimeCapabilityEnvelope();
  valid.gateway.mode = "observe";
  valid.gateway.features.listener = runtimeFeature({
    enabled: false,
    configured: false,
    operational: "disabled",
  });
  assert.equal(normalizeRuntimeCapability(valid).gateway.mode, "observe");

  const mutations = [
    { enabled: true },
    { enabled: "unknown" },
    { configured: true },
    { configured: "unknown" },
    { operational: "starting" },
    { operational: "ready" },
    { operational: "degraded" },
    { operational: "unavailable" },
    { operational: "unknown" },
  ];
  for (const mutation of mutations) {
    const input = structuredClone(valid);
    Object.assign(input.gateway.features.listener, mutation);
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(normalized.observer, safeObserver(), JSON.stringify(mutation));
    assert.deepEqual(normalized.gateway, unknownGateway(), JSON.stringify(mutation));
  }
});

test("observe mode preserves an unsupported listener as explicitly disabled", () => {
  const { normalizeRuntimeCapability } = require("../runtime_capability.js");
  const input = runtimeCapabilityEnvelope();
  input.gateway.mode = "observe";
  input.gateway.features.listener = runtimeFeature({
    support: "unsupported",
    enabled: false,
    configured: false,
    operational: "disabled",
  });

  const normalized = normalizeRuntimeCapability(input);
  assert.deepEqual(normalized.gateway.features.listener, {
    support: "unsupported",
    enabled: false,
    configured: false,
    operational: "disabled",
  });
  assert.deepEqual(normalizeRuntimeCapability(normalized), normalized);
});

test("injects distinct private tokens and returns only bounded sanitized responses", async (context) => {
  const {
    classifyRendererRequest,
    fetchRendererResponse,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-proxy-")),
  );
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.chmodSync(root, 0o700);
  const apiToken = "a".repeat(48);
  const gatewayToken = "g".repeat(48);
  const apiTokenPath = path.join(root, "api.token");
  const gatewayTokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(apiTokenPath, apiToken, { encoding: "ascii", mode: 0o600 });
  fs.writeFileSync(gatewayTokenPath, gatewayToken, {
    encoding: "ascii",
    mode: 0o600,
  });

  const localRequests = [];
  const localServer = http.createServer((request, response) => {
    localRequests.push({ url: request.url, headers: { ...request.headers } });
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("Cache-Control", "private, no-cache");
    response.setHeader("ETag", 'W/"observer-etag"');
    response.setHeader("Set-Cookie", "private-cookie=must-not-cross");
    response.setHeader("Authorization", "Bearer must-not-cross");
    response.setHeader("X-Private-Upstream", "must-not-cross");
    if (request.headers.authorization !== `Bearer ${apiToken}`) {
      response.statusCode = 401;
      response.end('{"error":{"code":"authentication_required"}}');
      return;
    }
    if (request.url === "/v1/health") {
      response.end(JSON.stringify({
        schemaVersion: "1.0",
        dataRevision: 31,
        generatedAt: "2026-08-09T03:04:05Z",
        health: { ok: true, status: "ok" },
      }));
      return;
    }
    response.end('{"schemaVersion":"1.0","dataRevision":31,"quotaWindows":[]}');
  });
  const gatewayRequests = [];
  const gatewayServer = http.createServer((request, response) => {
    gatewayRequests.push({ url: request.url, headers: { ...request.headers } });
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("Cache-Control", "no-store");
    response.setHeader("Vary", "Accept");
    response.setHeader("Set-Cookie", "gateway-cookie=must-not-cross");
    response.setHeader("X-Private-Upstream", "must-not-cross");
    if (request.headers.authorization !== `Bearer ${gatewayToken}`) {
      response.statusCode = 401;
      response.end('{"error":{"code":"authentication_required"}}');
      return;
    }
    response.end(JSON.stringify(runtimeCapabilityEnvelope()));
  });
  const localPort = await listenTcp(localServer);
  const gatewayPort = await listenTcp(gatewayServer);
  context.after(async () => {
    await Promise.all([closeServer(localServer), closeServer(gatewayServer)]);
  });
  const runtime = {
    localApi: {
      transport: "tcp",
      host: "127.0.0.1",
      port: localPort,
      tokenPath: apiTokenPath,
    },
    gateway: {
      transport: "tcp",
      host: "127.0.0.1",
      port: gatewayPort,
      tokenPath: gatewayTokenPath,
    },
  };

  const localResult = await fetchRendererResponse(
    classifyRendererRequest({
      method: "GET",
      target: "/v1/snapshot",
      headers: { "if-none-match": 'W/"browser-etag"' },
    }),
    {
      runtime,
      platform: process.platform,
      verifyWindowsAcl: approveFixtureWindowsAcl,
    },
  );
  const capabilityResult = await fetchRendererResponse(
    classifyRendererRequest({
      method: "GET",
      target: "/gateway/v1/health",
      headers: {},
    }),
    {
      runtime,
      platform: process.platform,
      verifyWindowsAcl: approveFixtureWindowsAcl,
    },
  );

  assert.equal(localResult.statusCode, 200);
  assert.deepEqual(localResult.headers, {
    "Cache-Control": "private, no-cache",
    "Content-Length": String(localResult.body.length),
    "Content-Type": "application/json; charset=utf-8",
    ETag: 'W/"observer-etag"',
    "X-Content-Type-Options": "nosniff",
  });
  assert.deepEqual(JSON.parse(localResult.body.toString("utf8")), {
    schemaVersion: "1.0",
    dataRevision: 31,
    quotaWindows: [],
  });
  assert.equal(capabilityResult.statusCode, 200);
  assert.deepEqual(capabilityResult.headers, {
    "Cache-Control": "no-store",
    "Content-Length": String(capabilityResult.body.length),
    "Content-Type": "application/json; charset=utf-8",
    Vary: "Accept",
    "X-Content-Type-Options": "nosniff",
  });
  const capability = JSON.parse(capabilityResult.body.toString("utf8"));
  assert.equal(capability.observer.operational, "ready");
  assert.equal(capability.observer.dataRevision, 31);
  assert.equal(capability.gateway.mode, "gateway");
  assert.doesNotMatch(
    JSON.stringify({ localResult, capabilityResult }),
    /must-not-cross|private-cookie|gateway-cookie/,
  );

  assert.equal(localRequests.length, 2);
  assert.deepEqual(
    localRequests.map((request) => request.headers.authorization),
    [`Bearer ${apiToken}`, `Bearer ${apiToken}`],
  );
  assert.equal(localRequests[0].headers["if-none-match"], 'W/"browser-etag"');
  assert.equal(localRequests[0].headers.accept, "application/json");
  assert.equal(gatewayRequests.length, 1);
  assert.equal(gatewayRequests[0].headers.authorization, `Bearer ${gatewayToken}`);
  assert.equal(
    gatewayRequests[0].headers.accept,
    "application/vnd.openusage.runtime-capability+json",
  );
  assert.equal(gatewayRequests[0].headers["if-none-match"], undefined);
});

test(
  "uses the owner-private Unix socket without bearer headers or aliased parents",
  { skip: process.platform === "win32" },
  async (context) => {
    const {
      classifyRendererRequest,
      fetchRendererResponse,
      validatePrivateUnixSocket,
    } = require("../gateway_proxy.js");
    assert.equal(typeof validatePrivateUnixSocket, "function");
    const root = fs.realpathSync(
      fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-unix-")),
    );
    fs.chmodSync(root, 0o700);
    const socketPath = path.join(root, "openusage.sock");
    const aliasRoot = `${root}-alias`;
    const requests = [];
    const server = http.createServer((request, response) => {
      requests.push({ ...request.headers });
      response.setHeader("Content-Type", "application/json; charset=utf-8");
      response.end('{"schemaVersion":"1.0","dataRevision":2,"sources":[]}');
    });
    await listenUnix(server, socketPath);
    fs.chmodSync(socketPath, 0o600);
    assert.equal(validatePrivateUnixSocket(socketPath), true);
    context.after(async () => {
      await closeServer(server);
      fs.rmSync(aliasRoot, { force: true });
      fs.rmSync(root, { recursive: true, force: true });
    });
    const classified = classifyRendererRequest({
      method: "GET",
      target: "/v1/sources/status",
      headers: {},
    });
    const runtime = {
      localApi: { transport: "unix", socketPath },
      gateway: null,
    };

    const result = await fetchRendererResponse(classified, {
      runtime,
      platform: process.platform,
    });
    assert.equal(result.statusCode, 200);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].host, "localhost");
    assert.equal(requests[0].accept, "application/json");
    assert.equal(requests[0].authorization, undefined);
    assert.equal(requests[0].cookie, undefined);

    fs.symlinkSync(root, aliasRoot, "dir");
    assert.equal(
      validatePrivateUnixSocket(path.join(aliasRoot, "openusage.sock")),
      false,
    );
    await assert.rejects(
      fetchRendererResponse(classified, {
        runtime: {
          ...runtime,
          localApi: {
            transport: "unix",
            socketPath: path.join(aliasRoot, "openusage.sock"),
          },
        },
        platform: process.platform,
      }),
      /^Error: private service unavailable$/,
    );
    assert.equal(requests.length, 1);
  },
);

test("never follows redirects and contains timeout or oversized Gateway failures", async (context) => {
  const {
    classifyRendererRequest,
    fetchRendererResponse,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-bounds-")),
  );
  fs.chmodSync(root, 0o700);
  const apiToken = "a".repeat(48);
  const gatewayToken = "g".repeat(48);
  const apiTokenPath = path.join(root, "api.token");
  const gatewayTokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(apiTokenPath, apiToken, { encoding: "ascii", mode: 0o600 });
  fs.writeFileSync(gatewayTokenPath, gatewayToken, {
    encoding: "ascii",
    mode: 0o600,
  });
  let trapHits = 0;
  const trapServer = http.createServer((_request, response) => {
    trapHits += 1;
    response.end("redirect followed");
  });
  const trapPort = await listenTcp(trapServer);
  const localServer = http.createServer((request, response) => {
    if (request.url === "/v1/snapshot") {
      response.statusCode = 302;
      response.setHeader("Location", `http://127.0.0.1:${trapPort}/private`);
      response.end();
      return;
    }
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.end(JSON.stringify({
      schemaVersion: "1.0",
      dataRevision: 41,
      generatedAt: "2026-08-09T04:05:06Z",
      health: { ok: true, status: "ok" },
    }));
  });
  let gatewayMode = "redirect";
  const gatewayServer = http.createServer((_request, response) => {
    if (gatewayMode === "slow") return;
    if (gatewayMode === "oversized-body") {
      response.end(Buffer.alloc(64 * 1024 + 1, 0x61));
      return;
    }
    if (gatewayMode === "oversized-header") {
      response.setHeader("X-Large", "x".repeat(20 * 1024));
      response.end(JSON.stringify(runtimeCapabilityEnvelope()));
      return;
    }
    response.statusCode = 302;
    response.setHeader("Location", `http://127.0.0.1:${trapPort}/gateway-private`);
    response.end();
  });
  const localPort = await listenTcp(localServer);
  const gatewayPort = await listenTcp(gatewayServer);
  context.after(async () => {
    await Promise.all([
      closeServer(localServer),
      closeServer(gatewayServer),
      closeServer(trapServer),
    ]);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const runtime = {
    localApi: {
      transport: "tcp",
      host: "127.0.0.1",
      port: localPort,
      tokenPath: apiTokenPath,
    },
    gateway: {
      transport: "tcp",
      host: "127.0.0.1",
      port: gatewayPort,
      tokenPath: gatewayTokenPath,
    },
  };
  const localRequest = classifyRendererRequest({
    method: "GET",
    target: "/v1/snapshot",
    headers: {},
  });
  const capabilityRequest = classifyRendererRequest({
    method: "GET",
    target: "/gateway/v1/health",
    headers: {},
  });

  const redirected = await fetchRendererResponse(localRequest, {
    runtime,
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
  });
  assert.equal(redirected.statusCode, 302);
  assert.equal(trapHits, 0);

  gatewayMode = "slow";
  const started = Date.now();
  const timedOut = await fetchRendererResponse(capabilityRequest, {
    runtime,
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
    deadlineMs: 50,
  });
  assert.ok(Date.now() - started < 1_000);
  assert.equal(JSON.parse(timedOut.body).observer.operational, "ready");
  assert.deepEqual(JSON.parse(timedOut.body).gateway.lastError, {
    code: "gateway_timeout",
    retryable: true,
  });

  for (const mode of ["oversized-body", "oversized-header"]) {
    gatewayMode = mode;
    const invalid = await fetchRendererResponse(capabilityRequest, {
      runtime,
      platform: process.platform,
      verifyWindowsAcl: approveFixtureWindowsAcl,
    });
    assert.equal(JSON.parse(invalid.body).observer.operational, "ready", mode);
    assert.deepEqual(
      JSON.parse(invalid.body).gateway.lastError,
      { code: "capability_invalid", retryable: false },
      mode,
    );
  }

  gatewayMode = "redirect";
  const gatewayRedirect = await fetchRendererResponse(capabilityRequest, {
    runtime,
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
  });
  assert.deepEqual(JSON.parse(gatewayRedirect.body).gateway.lastError, {
    code: "gateway_unavailable",
    retryable: true,
  });
  assert.equal(trapHits, 0);
});

test("serves the renderer API boundary without reflecting credentials or private errors", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  assert.equal(typeof createRendererApiHandler, "function");
  assert.equal(typeof isRendererApiTarget, "function");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-handler-")),
  );
  fs.chmodSync(root, 0o700);
  const apiTokenPath = path.join(root, "api.token");
  fs.writeFileSync(apiTokenPath, "a".repeat(48), {
    encoding: "ascii",
    mode: 0o600,
  });
  let upstreamHits = 0;
  const upstreamBody = '{"health":{"ok":true,"status":"ok"}}';
  const upstream = http.createServer((request, response) => {
    upstreamHits += 1;
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("Content-Length", String(Buffer.byteLength(upstreamBody)));
    response.setHeader("Set-Cookie", "private=must-not-cross");
    response.end(request.method === "HEAD" ? undefined : upstreamBody);
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath: apiTokenPath,
      },
      gateway: null,
    },
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
  });
  const rendererServer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(rendererServer);
  context.after(async () => {
    await Promise.all([closeServer(rendererServer), closeServer(upstream)]);
    fs.rmSync(root, { recursive: true, force: true });
  });

  const ok = await requestTcp(rendererPort, "/v1/health");
  assert.equal(ok.statusCode, 200);
  assert.deepEqual(JSON.parse(ok.body.toString("utf8")), {
    health: { ok: true, status: "ok" },
  });
  assert.equal(ok.headers["set-cookie"], undefined);
  assert.equal(ok.headers.authorization, undefined);

  const head = await requestTcp(rendererPort, "/v1/health", {
    method: "HEAD",
  });
  assert.equal(head.statusCode, 200);
  assert.equal(head.body.length, 0);
  assert.equal(
    head.headers["content-length"],
    String(Buffer.byteLength(upstreamBody)),
  );

  const credentialed = await requestTcp(rendererPort, "/v1/health", {
    headers: { Authorization: "Bearer renderer-must-not-cross" },
  });
  assert.equal(credentialed.statusCode, 404);
  assert.doesNotMatch(credentialed.body.toString("utf8"), /renderer-must-not-cross/);
  const writeAttempt = await requestTcp(rendererPort, "/v1/health", {
    method: "POST",
  });
  assert.equal(writeAttempt.statusCode, 404);
  assert.equal(upstreamHits, 2);

  fs.unlinkSync(apiTokenPath);
  const unavailable = await requestTcp(rendererPort, "/v1/health");
  assert.equal(unavailable.statusCode, 502);
  assert.deepEqual(JSON.parse(unavailable.body.toString("utf8")), {
    error: { code: "service_unavailable" },
  });
  assert.doesNotMatch(
    unavailable.body.toString("utf8"),
    /api\.token|private token|ENOENT|openusage-desktop-handler/,
  );
});

test("bridges one canonical bounded Should-Send request without exposing private state", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-advice-")),
  );
  fs.chmodSync(root, 0o700);
  const gatewayToken = "s".repeat(48);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, gatewayToken, { encoding: "ascii", mode: 0o600 });
  const privateCanary = "PRIVATE_RAW_ERROR_TOKEN_PATH_41f8";
  const upstreamRequests = [];
  const upstream = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      upstreamRequests.push({
        method: request.method,
        url: request.url,
        headers: { ...request.headers },
        body: Buffer.concat(chunks).toString("utf8"),
      });
      response.setHeader("Content-Type", "application/json; charset=utf-8");
      response.setHeader("Set-Cookie", `${privateCanary}=1`);
      response.setHeader("X-Private-Path", tokenPath);
      response.end(JSON.stringify({
        decision: "defer",
        confidence: 0.8,
        reason: "quota_low",
        defer_until: null,
        raw_error: privateCanary,
        credential: privateCanary,
        details: {
          quota_remaining: null,
          burn_rate_per_min: 10,
          predicted_exhaustion_minutes: null,
          private_path: tokenPath,
        },
      }));
    });
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: null,
      gateway: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath,
      },
    },
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
  });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await Promise.all([closeServer(renderer), closeServer(upstream)]);
    fs.rmSync(root, { recursive: true, force: true });
  });

  const submitted = JSON.stringify({
    window: "5m",
    estimated_tokens: 8000,
    model: "gpt-4o",
    provider: "openai",
  });
  const result = await requestTcp(rendererPort, "/gateway/v1/should-send", {
    method: "POST",
    headers: {
      Accept: "text/html",
      "Content-Length": String(Buffer.byteLength(submitted)),
      "Content-Type": "application/json",
      "X-Renderer-Canary": privateCanary,
    },
    body: submitted,
  });

  assert.equal(result.statusCode, 200);
  assert.deepEqual(JSON.parse(result.body.toString("utf8")), {
    decision: "defer",
    confidence: 0.8,
    reason: "quota_low",
    defer_until: null,
    details: {
      quota_remaining: null,
      burn_rate_per_min: 10,
      predicted_exhaustion_minutes: null,
    },
  });
  assert.equal(result.headers["set-cookie"], undefined);
  assert.equal(result.headers["x-private-path"], undefined);
  assert.doesNotMatch(JSON.stringify(result), new RegExp(privateCanary));
  assert.equal(upstreamRequests.length, 1);
  assert.deepEqual(upstreamRequests[0], {
    method: "POST",
    url: "/gateway/v1/should-send",
    headers: {
      accept: "application/json",
      authorization: `Bearer ${gatewayToken}`,
      connection: "close",
      "content-length": String(Buffer.byteLength(JSON.stringify({
        provider: "openai",
        model: "gpt-4o",
        estimated_tokens: 8000,
        window: "5m",
      }))),
      "content-type": "application/json",
      host: `127.0.0.1:${upstreamPort}`,
    },
    body: JSON.stringify({
      provider: "openai",
      model: "gpt-4o",
      estimated_tokens: 8000,
      window: "5m",
    }),
  });
});

test("normalizes optional Should-Send timing before it crosses the renderer bridge", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-advice-timing-")),
  );
  fs.chmodSync(root, 0o700);
  const gatewayToken = "u".repeat(48);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, gatewayToken, { encoding: "ascii", mode: 0o600 });
  const privateCanary = "PRIVATE_UNSAFE_DEFER_UNTIL_ef16";
  const details = {
    quota_remaining: 1_000,
    burn_rate_per_min: 10,
    predicted_exhaustion_minutes: 100,
  };
  const cases = [
    {
      upstream: {
        decision: "defer",
        confidence: 0.8,
        reason: "quota_low",
        details,
      },
      expectedDeferUntil: null,
    },
    {
      upstream: {
        decision: "defer",
        confidence: 0.8,
        reason: "quota_low",
        defer_until: privateCanary,
        details,
      },
      expectedDeferUntil: null,
    },
    {
      upstream: {
        decision: "defer",
        confidence: 0.8,
        reason: "quota_low",
        defer_until: "2026-08-09T10:15:30.123456+08:00",
        details,
      },
      expectedDeferUntil: "2026-08-09T10:15:30.123456+08:00",
    },
    {
      upstream: {
        decision: "yes",
        confidence: 0.9,
        reason: "quota_healthy",
        defer_until: "2026-08-09T10:15:00Z",
        details,
      },
      expectedDeferUntil: null,
    },
    {
      upstream: {
        decision: "no",
        confidence: 0.9,
        reason: "approaching_limit",
        defer_until: "2026-08-09T10:15:00Z",
        details,
      },
      expectedDeferUntil: null,
    },
  ];
  let upstreamHits = 0;
  const upstream = http.createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      const testCase = cases[upstreamHits];
      upstreamHits += 1;
      response.setHeader("Content-Type", "application/json; charset=utf-8");
      response.end(JSON.stringify({
        ...testCase.upstream,
        raw_private_state: privateCanary,
      }));
    });
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: null,
      gateway: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath,
      },
    },
    platform: process.platform,
    verifyWindowsAcl: approveFixtureWindowsAcl,
  });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await Promise.all([closeServer(renderer), closeServer(upstream)]);
    fs.rmSync(root, { recursive: true, force: true });
  });

  const submitted = JSON.stringify({
    window: "5m",
    estimated_tokens: 8_000,
    model: "gpt-4o",
    provider: "openai",
  });
  for (const testCase of cases) {
    const result = await requestTcp(rendererPort, "/gateway/v1/should-send", {
      method: "POST",
      headers: {
        "Content-Length": String(Buffer.byteLength(submitted)),
        "Content-Type": "application/json",
      },
      body: submitted,
    });

    assert.equal(result.statusCode, 200);
    assert.deepEqual(JSON.parse(result.body.toString("utf8")), {
      decision: testCase.upstream.decision,
      confidence: testCase.upstream.confidence,
      reason: testCase.upstream.reason,
      defer_until: testCase.expectedDeferUntil,
      details,
    });
    assert.doesNotMatch(result.body.toString("utf8"), new RegExp(privateCanary));
  }
  assert.equal(upstreamHits, cases.length);
});

test("rejects malformed or oversized Should-Send renderer writes before Gateway", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  let upstreamHits = 0;
  const upstream = http.createServer((_request, response) => {
    upstreamHits += 1;
    response.end("{}");
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: null,
      gateway: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath: "/private/CANARY_GATEWAY_TOKEN_PATH",
      },
    },
    platform: process.platform,
  });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await Promise.all([closeServer(renderer), closeServer(upstream)]);
  });

  const cases = [
    {
      target: "/gateway/v1/should-send?debug=1",
      body: "{}",
      headers: { "Content-Type": "application/json" },
    },
    {
      target: "/gateway/v1/should-send",
      body: JSON.stringify({
        provider: "openai",
        model: "gpt-4o",
        estimated_tokens: 0,
        window: "5m",
        credential: "CANARY_RENDERER_CREDENTIAL",
      }),
      headers: { "Content-Type": "application/json" },
    },
    {
      target: "/gateway/v1/should-send",
      body: "x".repeat(8 * 1024 + 1),
      headers: { "Content-Type": "application/json" },
    },
    {
      target: "/gateway/v1/should-send",
      body: "{}",
      headers: {
        Authorization: "Bearer CANARY_RENDERER_TOKEN",
        "Content-Type": "application/json",
      },
    },
    {
      target: "/gateway/v1/should-send",
      body: "{}",
      headers: { "Content-Type": "text/plain" },
    },
  ];
  for (const item of cases) {
    const result = await requestTcp(rendererPort, item.target, {
      method: "POST",
      headers: {
        ...item.headers,
        "Content-Length": String(Buffer.byteLength(item.body)),
      },
      body: item.body,
    });
    assert.ok(result.statusCode >= 400, item.target);
    assert.doesNotMatch(
      result.body.toString("utf8"),
      /CANARY|credential|token path|private token|ENOENT/i,
    );
  }
  assert.equal(upstreamHits, 0);
});

test("canonicalizes one integer lexeme and rejects ambiguous or non-integer spellings", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-advice-integer-")),
  );
  fs.chmodSync(root, 0o700);
  const gatewayToken = "i".repeat(48);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, gatewayToken, { encoding: "ascii", mode: 0o600 });
  let upstreamHits = 0;
  const upstreamBodies = [];
  const upstream = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      upstreamHits += 1;
      upstreamBodies.push(Buffer.concat(chunks).toString("utf8"));
      response.setHeader("Content-Type", "application/json; charset=utf-8");
      response.end(JSON.stringify({
        decision: "yes",
        confidence: 1,
        reason: "quota_healthy",
        defer_until: null,
        details: {
          quota_remaining: null,
          burn_rate_per_min: null,
          predicted_exhaustion_minutes: null,
        },
      }));
    });
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: null,
      gateway: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath,
      },
    },
    platform: process.platform,
    verifyWindowsAcl:
      process.platform === "win32" ? () => true : undefined,
  });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await Promise.all([closeServer(renderer), closeServer(upstream)]);
    fs.rmSync(root, { recursive: true, force: true });
  });

  const acceptedBody =
    '{ "window" : "5m", "estimated_\\u0074okens" : 1, ' +
    '"model" : "gpt-4o", "provider" : "openai" }';
  const accepted = await requestTcp(
    rendererPort,
    "/gateway/v1/should-send",
    {
      method: "POST",
      headers: {
        "Content-Length": String(Buffer.byteLength(acceptedBody)),
        "Content-Type": "application/json",
      },
      body: acceptedBody,
    },
  );
  assert.equal(accepted.statusCode, 200);
  assert.deepEqual(JSON.parse(accepted.body.toString("utf8")), {
    decision: "yes",
    confidence: 1,
    reason: "quota_healthy",
    defer_until: null,
    details: {
      quota_remaining: null,
      burn_rate_per_min: null,
      predicted_exhaustion_minutes: null,
    },
  });
  assert.deepEqual(upstreamBodies, [JSON.stringify({
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 1,
    window: "5m",
  })]);

  const results = [];
  for (const numberToken of ["1.0", "1e0", "2147483647.0000001"]) {
    const body =
      `{"provider":"openai","model":"gpt-4o",` +
      `"estimated_tokens":${numberToken},"window":"5m"}`;
    results.push(await requestTcp(rendererPort, "/gateway/v1/should-send", {
      method: "POST",
      headers: {
        "Content-Length": String(Buffer.byteLength(body)),
        "Content-Type": "application/json",
      },
      body,
    }));
  }
  const duplicateBody =
    '{"provider":"openai","model":"gpt-4o","estimated_tokens":1,' +
    '"estimated_\\u0074okens":2,"window":"5m"}';
  results.push(await requestTcp(rendererPort, "/gateway/v1/should-send", {
    method: "POST",
    headers: {
      "Content-Length": String(Buffer.byteLength(duplicateBody)),
      "Content-Type": "application/json",
    },
    body: duplicateBody,
  }));

  assert.deepEqual(
    results.map(({ statusCode }) => statusCode),
    [400, 400, 400, 400],
  );
  for (const result of results) {
    assert.deepEqual(JSON.parse(result.body.toString("utf8")), {
      error: { code: "invalid_request" },
    });
  }
  assert.equal(upstreamHits, 1);
  assert.equal(upstreamBodies.length, 1);
});

test("rejects an upstream non-200 Should-Send decision with one stable problem", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-advice-status-")),
  );
  fs.chmodSync(root, 0o700);
  const gatewayToken = "n".repeat(48);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, gatewayToken, { encoding: "ascii", mode: 0o600 });
  const privateCanary = "PRIVATE_NON_200_GATEWAY_ERROR_8a31";
  const upstream = http.createServer((_request, response) => {
    response.statusCode = 201;
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("X-Private-Path", tokenPath);
    response.end(JSON.stringify({
      decision: "defer",
      confidence: 0.8,
      reason: "quota_low",
      defer_until: null,
      raw_error: privateCanary,
      details: {
        quota_remaining: null,
        burn_rate_per_min: 10,
        predicted_exhaustion_minutes: null,
      },
    }));
  });
  const upstreamPort = await listenTcp(upstream);
  const handler = createRendererApiHandler({
    runtime: {
      localApi: null,
      gateway: {
        transport: "tcp",
        host: "127.0.0.1",
        port: upstreamPort,
        tokenPath,
      },
    },
    platform: process.platform,
    verifyWindowsAcl:
      process.platform === "win32" ? () => true : undefined,
  });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await Promise.all([closeServer(renderer), closeServer(upstream)]);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const body = JSON.stringify({
    provider: "openai",
    model: "gpt-4o",
    estimated_tokens: 1,
    window: "5m",
  });

  const result = await requestTcp(rendererPort, "/gateway/v1/should-send", {
    method: "POST",
    headers: {
      "Content-Length": String(Buffer.byteLength(body)),
      "Content-Type": "application/json",
    },
    body,
  });

  assert.equal(result.statusCode, 502);
  assert.deepEqual(JSON.parse(result.body.toString("utf8")), {
    error: { code: "service_unavailable" },
  });
  assert.equal(result.headers["x-private-path"], undefined);
  assert.doesNotMatch(JSON.stringify(result), new RegExp(privateCanary));
});

test("closes a stalled partial Should-Send body after its bounded problem", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const handler = createRendererApiHandler({ runtime: null, deadlineMs: 50 });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  const socket = net.createConnection({
    host: "127.0.0.1",
    port: rendererPort,
  });
  context.after(async () => {
    socket.destroy();
    await closeServer(renderer);
  });
  let rawResponse = "";
  let responseSeen;
  const responseReady = new Promise((resolve) => {
    responseSeen = resolve;
  });
  let socketClosed;
  const closed = new Promise((resolve) => {
    socketClosed = resolve;
  });
  socket.on("data", (chunk) => {
    rawResponse += chunk.toString("utf8");
    if (rawResponse.includes('{"error":{"code":"service_unavailable"}}')) {
      responseSeen();
    }
  });
  socket.on("error", () => {
    // A reset is an acceptable bounded close after the stable problem is sent.
  });
  socket.on("close", () => socketClosed());
  await new Promise((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", reject);
  });
  socket.write(
    `POST /gateway/v1/should-send HTTP/1.1\r\n` +
      `Host: 127.0.0.1:${rendererPort}\r\n` +
      "Content-Type: application/json\r\n" +
      "Content-Length: 100\r\n" +
      "Connection: keep-alive\r\n\r\n" +
      "{",
  );

  const sawProblem = await Promise.race([
    responseReady.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 1_000)),
  ]);
  assert.equal(sawProblem, true);
  assert.match(rawResponse, /^HTTP\/1\.1 502 /u);
  const didClose = await Promise.race([
    closed.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 500)),
  ]);
  assert.equal(didClose, true);
});

test("closes a stalled Should-Send body rejected during classification", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const handler = createRendererApiHandler({ runtime: null, deadlineMs: 50 });
  const renderer = http.createServer((request, response) => {
    if (!isRendererApiTarget(request.url)) {
      response.statusCode = 404;
      response.end();
      return;
    }
    handler(request, response);
  });
  const rendererPort = await listenTcp(renderer);
  context.after(async () => {
    await closeServer(renderer);
  });
  const cases = [
    {
      name: "exact path with a rejected media type",
      target: "/gateway/v1/should-send",
      contentType: "text/plain",
    },
    {
      name: "query path with an otherwise valid media type",
      target: "/gateway/v1/should-send?debug=1",
      contentType: "application/json",
    },
  ];
  for (const item of cases) {
    const socket = net.createConnection({
      host: "127.0.0.1",
      port: rendererPort,
    });
    try {
      let rawResponse = "";
      let responseSeen;
      const responseReady = new Promise((resolve) => {
        responseSeen = resolve;
      });
      let socketClosed;
      const closed = new Promise((resolve) => {
        socketClosed = resolve;
      });
      socket.on("data", (chunk) => {
        rawResponse += chunk.toString("utf8");
        if (rawResponse.includes('{"error":{"code":"not_found"}}')) {
          responseSeen();
        }
      });
      socket.on("error", () => {
        // A reset is an acceptable bounded close after the stable problem is sent.
      });
      socket.on("close", () => socketClosed());
      await new Promise((resolve, reject) => {
        socket.once("connect", resolve);
        socket.once("error", reject);
      });
      socket.write(
        `POST ${item.target} HTTP/1.1\r\n` +
          `Host: 127.0.0.1:${rendererPort}\r\n` +
          `Content-Type: ${item.contentType}\r\n` +
          "Content-Length: 100\r\n" +
          "Connection: keep-alive\r\n\r\n" +
          "{",
      );

      const sawProblem = await Promise.race([
        responseReady.then(() => true),
        new Promise((resolve) => setTimeout(() => resolve(false), 1_000)),
      ]);
      assert.equal(sawProblem, true, item.name);
      assert.match(rawResponse, /^HTTP\/1\.1 404 /u, item.name);
      const didClose = await Promise.race([
        closed.then(() => true),
        new Promise((resolve) => setTimeout(() => resolve(false), 500)),
      ]);
      assert.equal(didClose, true, item.name);
    } finally {
      socket.destroy();
    }
  }
});

test("serves static renderer files only from WEB_ROOT with safe failures", async (context) => {
  const { createStaticFileHandler } = require("../gateway_proxy.js");
  assert.equal(typeof createStaticFileHandler, "function");
  const workspace = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-desktop-static-")),
  );
  const webRoot = path.join(workspace, "web-root");
  fs.mkdirSync(webRoot);
  const indexBody = "<!doctype html><title>UsageHub</title>";
  const scriptBody = "globalThis.OPENUSAGE_STATIC = true;";
  const outsideBody = "CANARY_OUTSIDE_WEB_ROOT";
  fs.writeFileSync(path.join(webRoot, "index.html"), indexBody);
  fs.writeFileSync(path.join(webRoot, "app.js"), scriptBody);
  const outsidePath = path.join(workspace, "outside.txt");
  fs.writeFileSync(outsidePath, outsideBody);
  if (process.platform !== "win32") {
    fs.symlinkSync(outsidePath, path.join(webRoot, "outside-alias.txt"));
  }

  const handler = createStaticFileHandler({
    webRoot,
    mimeTypes: {
      ".html": "text/html; charset=utf-8",
      ".js": "text/javascript",
    },
  });
  const server = http.createServer(handler);
  const port = await listenTcp(server);
  context.after(async () => {
    await closeServer(server);
    fs.rmSync(workspace, { recursive: true, force: true });
  });

  const asset = await requestTcp(port, "/app.js?revision=1");
  assert.equal(asset.statusCode, 200);
  assert.equal(asset.body.toString("utf8"), scriptBody);
  assert.equal(asset.headers["content-type"], "text/javascript");

  const route = await requestTcp(port, "/settings/providers");
  assert.equal(route.statusCode, 200);
  assert.equal(route.body.toString("utf8"), indexBody);

  const head = await requestTcp(port, "/app.js", { method: "HEAD" });
  assert.equal(head.statusCode, 200);
  assert.equal(head.body.length, 0);
  assert.equal(head.headers["content-length"], String(Buffer.byteLength(scriptBody)));

  const malformed = await requestTcp(port, "/%E0%A4%A");
  assert.equal(malformed.statusCode, 400);
  assert.doesNotMatch(malformed.body.toString("utf8"), /openusage-desktop-static/);

  for (const target of [
    "/%2e%2e/outside.txt",
    "/..%2Foutside.txt",
    "/%2e%2e%2Foutside.txt",
  ]) {
    const traversal = await requestTcp(port, target);
    assert.equal(traversal.statusCode, 404, target);
    assert.doesNotMatch(traversal.body.toString("utf8"), /CANARY|outside\.txt|openusage-desktop-static/);
  }

  if (process.platform !== "win32") {
    const alias = await requestTcp(port, "/outside-alias.txt");
    assert.equal(alias.statusCode, 404);
    assert.doesNotMatch(alias.body.toString("utf8"), /CANARY|outside\.txt|openusage-desktop-static/);
  }
});

function runtimeFeature(overrides = {}) {
  return {
    support: "supported",
    enabled: true,
    configured: true,
    operational: "ready",
    ...overrides,
  };
}

function runtimeFeatures() {
  return {
    listener: runtimeFeature(),
    should_send: runtimeFeature({ operational: "starting" }),
    responses: runtimeFeature({ configured: false, operational: "starting" }),
    cache: runtimeFeature({ enabled: false, operational: "disabled" }),
    fallback: runtimeFeature({
      support: "unknown",
      enabled: "unknown",
      configured: "unknown",
      operational: "unknown",
    }),
    pii_redaction: runtimeFeature({ operational: "degraded" }),
    streaming: runtimeFeature({ operational: "degraded" }),
  };
}

function safeObserver() {
  return {
    operational: "ready",
    generatedAt: "2026-08-09T01:02:03Z",
    lastGoodAt: "2026-08-09T01:02:03Z",
    dataRevision: 17,
    schemaVersion: "1.0",
  };
}

function runtimeCapabilityEnvelope() {
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: safeObserver(),
    gateway: {
      mode: "gateway",
      operational: "degraded",
      features: runtimeFeatures(),
      configuredProviderCount: 2,
      healthyProviderCount: 1,
      actions: ["retry", "open_settings", "unknown_action", "retry"],
      lastError: {
        code: "provider_unavailable",
        retryable: true,
        observedAt: "2026-08-09T01:00:00Z",
      },
    },
  };
}

function crossRuntimeCapabilityEnvelope() {
  const envelope = runtimeCapabilityEnvelope();
  envelope.observer = {
    operational: "ready",
    generatedAt: "2026-08-08T15:00:00Z",
    lastGoodAt: "2026-08-08T14:59:00Z",
    dataRevision: 42,
    schemaVersion: "openusage/v1",
  };
  envelope.gateway.actions = [
    "clear_cache",
    "learn_more",
    "retry",
    "open_settings",
    "disable",
    "enable",
    "retry",
    "unknown_action",
  ];
  envelope.gateway.lastError.observedAt = "2026-08-08T14:58:00Z";
  envelope.gateway.adapterCount = 5;
  envelope.gateway.dispatchEvents = true;
  return envelope;
}

function safeRuntimeCapabilityEnvelope() {
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: safeObserver(),
    gateway: {
      mode: "gateway",
      operational: "degraded",
      features: runtimeFeatures(),
      configuredProviderCount: 2,
      healthyProviderCount: 1,
      actions: ["open_settings", "retry"],
      lastError: {
        code: "provider_unavailable",
        retryable: true,
        observedAt: "2026-08-09T01:00:00Z",
      },
    },
  };
}

function unknownGateway() {
  const feature = {
    support: "unknown",
    enabled: "unknown",
    configured: "unknown",
    operational: "unknown",
  };
  return {
    mode: "unknown",
    operational: "unknown",
    features: Object.fromEntries(
      Object.keys(runtimeFeatures()).map((featureId) => [featureId, { ...feature }]),
    ),
    configuredProviderCount: null,
    healthyProviderCount: null,
    actions: ["retry"],
    lastError: { code: "capability_invalid", retryable: false },
  };
}

function listenTcp(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

function listenUnix(server, socketPath) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close(() => resolve());
  });
}

function requestTcp(
  port,
  target,
  { method = "GET", headers = {}, body } = {},
) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        hostname: "127.0.0.1",
        port,
        path: target,
        method,
        headers,
        agent: false,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => resolve({
          statusCode: response.statusCode,
          headers: response.headers,
          body: Buffer.concat(chunks),
        }));
      },
    );
    request.on("error", reject);
    request.end(body);
  });
}
