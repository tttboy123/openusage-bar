import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  PLUGIN_CONNECTIONS_ROUTE,
  classifyPluginConnectionRequest,
  createPluginConnectionHandler,
  discoverPrivatePluginRuntime,
  fetchPrivatePluginConnections,
  isPluginConnectionRendererTarget,
  sanitizePluginConnectionsPayload,
  sanitizePluginConnectionsValue,
} = require("../plugin_connection_proxy.js");

const timestamp = "2026-08-10T03:04:05.123456Z";

function connectionPayload() {
  return {
    apiVersion: "plugin.openusage/v1",
    object: "plugin.connections",
    observedAt: timestamp,
    connections: [
      {
        pluginId: "loom",
        configuration: "configured",
        connection: "connected",
        capabilityState: "negotiated",
        capabilities: [
          "decision.lookup",
          "health.query",
          "outcome.record",
          "quotas.query",
          "route.advice",
          "usage.query",
        ],
        lastSeenAt: timestamp,
        lastSyncOutcome: "succeeded",
        lastSyncAt: timestamp,
      },
      {
        pluginId: "codex",
        configuration: "not_configured",
        connection: "never_seen",
        capabilityState: "not_negotiated",
        capabilities: [],
        lastSeenAt: null,
        lastSyncOutcome: "never",
        lastSyncAt: null,
      },
      {
        pluginId: "claude_code",
        configuration: "unknown",
        connection: "unknown",
        capabilityState: "unknown",
        capabilities: [],
        lastSeenAt: null,
        lastSyncOutcome: "unknown",
        lastSyncAt: null,
      },
    ],
  };
}

test("plugin connection discovery is independent and uses only the desktop token", () => {
  assert.deepEqual(
    discoverPrivatePluginRuntime({
      platform: "darwin",
      homeDir: "/Users/tester",
    }),
    {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17824,
      tokenPath:
        "/Users/tester/.local/state/openusage-bar/plugin/desktop.token",
    },
  );
  assert.deepEqual(
    discoverPrivatePluginRuntime({
      platform: "win32",
      localAppData: "C:\\Users\\tester\\AppData\\Local",
    }),
    {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17824,
      tokenPath:
        "C:\\Users\\tester\\AppData\\Local\\openusage-bar\\plugin\\desktop.token",
    },
  );
});

test("renderer exposes exactly one query-free GET plugin connection route", () => {
  assert.equal(PLUGIN_CONNECTIONS_ROUTE, "/host/v1/plugin-connections");
  assert.equal(
    isPluginConnectionRendererTarget("/host/v1/plugin-connections"),
    true,
  );
  assert.equal(isPluginConnectionRendererTarget("/plugin/v1/connections"), false);

  assert.deepEqual(
    classifyPluginConnectionRequest({
      method: "GET",
      target: "/host/v1/plugin-connections",
      headers: { accept: "application/json" },
    }),
    { method: "GET", target: "/plugin/v1/connections" },
  );

  for (const request of [
    { method: "HEAD", target: "/host/v1/plugin-connections" },
    { method: "POST", target: "/host/v1/plugin-connections" },
    { method: "GET", target: "/host/v1/plugin-connections?debug=1" },
    { method: "GET", target: "/host/v1/plugin-connections/" },
    { method: "GET", target: "/plugin/v1/connections" },
    {
      method: "GET",
      target: "/host/v1/plugin-connections",
      headers: { authorization: "Bearer renderer-controlled" },
    },
  ]) {
    assert.equal(classifyPluginConnectionRequest(request), null);
  }
});

test("plugin projection sanitizer returns only the exact public contract", () => {
  const source = connectionPayload();
  const sanitized = sanitizePluginConnectionsValue(source);
  assert.deepEqual(sanitized, source);
  assert.notEqual(sanitized, source);
  assert.notEqual(sanitized.connections[0], source.connections[0]);

  const bytes = Buffer.from(JSON.stringify(source), "utf8");
  assert.deepEqual(sanitizePluginConnectionsPayload(bytes), source);
});

test("plugin projection sanitizer fails closed on additive, unordered, or invalid state", () => {
  const mutations = [
    (value) => {
      value.privateToken = "secret";
    },
    (value) => {
      value.connections[0].endpoint = "http://private.invalid";
    },
    (value) => {
      value.connections.reverse();
    },
    (value) => {
      value.connections[0].capabilities.reverse();
    },
    (value) => {
      value.connections[0].capabilities.push("credentials.read");
    },
    (value) => {
      value.connections[0].lastSyncOutcome = "never";
    },
    (value) => {
      value.connections[0].lastSyncAt = null;
    },
    (value) => {
      value.connections[0].configuration = "unknown";
    },
    (value) => {
      value.connections[0].connection = "stale";
      value.connections[0].configuration = "unknown";
    },
    (value) => {
      value.connections[0].lastSeenAt = null;
    },
    (value) => {
      value.observedAt = "2026-08-10T03:04:05.123Z";
    },
  ];
  for (const mutate of mutations) {
    const value = connectionPayload();
    mutate(value);
    assert.equal(sanitizePluginConnectionsValue(value), null);
  }

  const oversized = Buffer.alloc(64 * 1024 + 1, 0x20);
  assert.equal(sanitizePluginConnectionsPayload(oversized), null);
});

test("plugin projection sanitizer rejects accessors, exotic prototypes, and hostile proxies", () => {
  const accessor = connectionPayload();
  Object.defineProperty(accessor.connections[0], "lastSeenAt", {
    enumerable: true,
    get() {
      throw new Error("must not execute");
    },
  });
  assert.equal(sanitizePluginConnectionsValue(accessor), null);

  const exotic = connectionPayload();
  Object.setPrototypeOf(exotic.connections[0], { privateToken: "secret" });
  assert.equal(sanitizePluginConnectionsValue(exotic), null);

  const symbolic = connectionPayload();
  symbolic.connections[0][Symbol("privateToken")] = "secret";
  assert.equal(sanitizePluginConnectionsValue(symbolic), null);

  const hostile = new Proxy(connectionPayload(), {
    getPrototypeOf() {
      throw new Error("hostile");
    },
  });
  assert.equal(sanitizePluginConnectionsValue(hostile), null);
});

test("desktop proxy uses only its private token and injects the Windows ACL seam", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "usagehub-plugin-client-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  fs.chmodSync(stateDir, 0o700);
  const tokenPath = path.join(stateDir, "desktop.token");
  const token = "d".repeat(43);
  fs.writeFileSync(tokenPath, token, { mode: 0o600 });

  let aclChecks = 0;
  let capturedOptions = null;
  const requestImpl = (options, callback) => {
    capturedOptions = options;
    const request = new EventEmitter();
    request.destroy = () => {};
    request.end = () => {
      queueMicrotask(() => {
        const response = new PassThrough();
        response.statusCode = 200;
        response.rawHeaders = ["Content-Type", "application/json"];
        response.headers = { "content-type": "application/json" };
        callback(response);
        response.end(Buffer.from(JSON.stringify(connectionPayload()), "utf8"));
      });
    };
    return request;
  };

  const result = await fetchPrivatePluginConnections({
    runtime: {
      transport: "tcp",
      host: "127.0.0.1",
      port: 17824,
      tokenPath,
    },
    platform: "win32",
    verifyWindowsAcl(receivedPath) {
      aclChecks += 1;
      assert.equal(receivedPath, tokenPath);
      return true;
    },
    requestImpl,
    deadlineMs: 500,
  });

  assert.deepEqual(result, connectionPayload());
  assert.equal(aclChecks, 1);
  assert.equal(capturedOptions.hostname, "127.0.0.1");
  assert.equal(capturedOptions.port, 17824);
  assert.equal(capturedOptions.path, "/plugin/v1/connections");
  assert.equal(capturedOptions.method, "GET");
  assert.equal(capturedOptions.headers.Authorization, `Bearer ${token}`);
  assert.equal(JSON.stringify(result).includes(token), false);
});

test("desktop proxy collapses invalid or oversized upstream data without echo", async () => {
  const requestImpl = (_options, callback) => {
    const request = new EventEmitter();
    request.destroy = () => {};
    request.end = () => {
      queueMicrotask(() => {
        const response = new PassThrough();
        response.statusCode = 200;
        response.rawHeaders = [];
        response.headers = {};
        callback(response);
        response.end(Buffer.alloc(64 * 1024 + 1, 0x61));
      });
    };
    return request;
  };
  await assert.rejects(
    fetchPrivatePluginConnections({
      runtime: {
        transport: "tcp",
        host: "127.0.0.1",
        port: 17824,
        tokenPath: "/definitely/not/a/token",
      },
      platform: "darwin",
      requestImpl,
    }),
    { message: "plugin connections unavailable" },
  );
});

function invokeHandler(handler, request = {}) {
  return new Promise((resolve) => {
    const headers = {};
    const chunks = [];
    const response = {
      writeHead(statusCode, responseHeaders) {
        response.statusCode = statusCode;
        Object.assign(headers, responseHeaders);
      },
      end(chunk) {
        if (chunk) chunks.push(Buffer.from(chunk));
        resolve({
          statusCode: response.statusCode,
          headers,
          body: Buffer.concat(chunks).toString("utf8"),
        });
      },
    };
    handler(
      {
        method: "GET",
        url: "/host/v1/plugin-connections",
        headers: {},
        headersDistinct: {},
        ...request,
      },
      response,
    );
  });
}

test("host handler exposes sanitized projection and never upstream metadata", async () => {
  let calls = 0;
  const handler = createPluginConnectionHandler({
    runtime: { private: "not returned" },
    async fetchConnections() {
      calls += 1;
      return connectionPayload();
    },
  });
  const result = await invokeHandler(handler);
  assert.equal(calls, 1);
  assert.equal(result.statusCode, 200);
  assert.equal(result.headers["Cache-Control"], "no-store");
  assert.equal(result.headers["Content-Type"], "application/json; charset=utf-8");
  assert.deepEqual(JSON.parse(result.body), connectionPayload());
  assert.equal(result.body.includes("private"), false);
});

test("host handler returns path-free stable errors and rejects method/query drift", async () => {
  const handler = createPluginConnectionHandler({
    async fetchConnections() {
      throw new Error("token=/private/plugin/desktop.token");
    },
  });
  const unavailable = await invokeHandler(handler);
  assert.deepEqual(
    { statusCode: unavailable.statusCode, body: JSON.parse(unavailable.body) },
    { statusCode: 502, body: { error: { code: "service_unavailable" } } },
  );
  for (const request of [
    { method: "POST" },
    { url: "/host/v1/plugin-connections?debug=1" },
    { url: "/plugin/v1/connections" },
  ]) {
    const rejected = await invokeHandler(handler, request);
    assert.equal(rejected.statusCode, 404);
    assert.deepEqual(JSON.parse(rejected.body), { error: { code: "not_found" } });
  }
});

test("desktop main and package include only the host projection proxy", () => {
  const desktopRoot = path.resolve(import.meta.dirname, "..");
  const main = fs.readFileSync(path.join(desktopRoot, "main.js"), "utf8");
  const packageJson = JSON.parse(
    fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  assert.match(main, /isPluginConnectionRendererTarget\(req\.url\)/);
  assert.match(main, /pluginConnectionHandler\(req, res\)/);
  assert.equal(
    packageJson.build.files.includes("plugin_connection_proxy.js"),
    true,
  );
  assert.equal(main.includes('isRendererApiTarget("/plugin/v1/'), false);
});
