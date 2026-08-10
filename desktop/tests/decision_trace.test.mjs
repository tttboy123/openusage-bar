import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const require = createRequire(import.meta.url);

function listenTcp(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

function safeDecisionTraces() {
  return {
    apiVersion: "gateway-decision-trace.openusage/v1",
    traces: [
      {
        traceId: "trace_00000000000000000000000000000003",
        occurredAt: "2026-08-10T10:00:03.000000Z",
        kind: "gateway_execution",
        execution: "executed",
        outcome: "succeeded",
        reason: null,
        pool: null,
        selected: { providerId: "openai", accountDisplayId: null },
        exclusions: [],
        fallback: {
          attempted: true,
          attemptCount: 2,
          finalAction: "retry",
        },
        factsWindow: null,
      },
      {
        traceId: "trace_00000000000000000000000000000002",
        occurredAt: "2026-08-10T10:00:02.000000Z",
        kind: "pool_selection",
        execution: "executed",
        outcome: "selected",
        reason: null,
        pool: { poolId: "daily-coding", revision: 3, strategy: "quota-aware" },
        selected: {
          providerId: "anthropic",
          accountDisplayId: "acct_0123456789ab",
        },
        exclusions: [
          { accountDisplayId: "acct_abcdef012345", reason: "cooldown" },
        ],
        fallback: null,
        factsWindow: null,
      },
      {
        traceId: "trace_00000000000000000000000000000001",
        occurredAt: "2026-08-10T10:00:01.000000Z",
        kind: "route_advice",
        execution: "advice_only",
        outcome: "defer",
        reason: "quota_unknown",
        pool: null,
        selected: null,
        exclusions: [],
        fallback: null,
        factsWindow: { durationSeconds: 300 },
      },
    ],
  };
}

test("classifies only the exact read-only Decision Trace route", () => {
  const { classifyRendererRequest } = require("../gateway_proxy.js");

  assert.deepEqual(
    classifyRendererRequest({
      method: "GET",
      target: "/gateway/v1/decision-traces",
      headers: { Accept: "text/html" },
    }),
    {
      service: "gateway",
      method: "GET",
      target: "/gateway/v1/decision-traces",
      headers: { Accept: "application/json" },
    },
  );

  for (const request of [
    { method: "HEAD", target: "/gateway/v1/decision-traces" },
    { method: "POST", target: "/gateway/v1/decision-traces" },
    { method: "GET", target: "/gateway/v1/decision-traces?limit=1" },
    {
      method: "GET",
      target: "/gateway/v1/decision-traces",
      headers: { "Content-Length": "2" },
    },
    {
      method: "GET",
      target: "/gateway/v1/decision-traces",
      headers: { "Transfer-Encoding": "chunked" },
    },
  ]) {
    assert.equal(
      classifyRendererRequest({ headers: {}, ...request }),
      null,
      `${request.method} ${request.target}`,
    );
  }
});

test("sanitizes the exact bounded Decision Trace projection", () => {
  const { sanitizeDecisionTracesPayload } = require("../gateway_proxy.js");
  assert.equal(typeof sanitizeDecisionTracesPayload, "function");

  const safe = safeDecisionTraces();
  assert.deepEqual(
    sanitizeDecisionTracesPayload(Buffer.from(JSON.stringify(safe), "utf8")),
    safe,
  );
  assert.deepEqual(
    sanitizeDecisionTracesPayload(Buffer.from(JSON.stringify({
      apiVersion: "gateway-decision-trace.openusage/v1",
      traces: [],
    }), "utf8")),
    { apiVersion: "gateway-decision-trace.openusage/v1", traces: [] },
  );

  const boundary = {
    apiVersion: "gateway-decision-trace.openusage/v1",
    traces: Array.from({ length: 128 }, (_item, index) => ({
      ...structuredClone(safe.traces[2]),
      traceId: `trace_${index.toString(16).padStart(32, "0")}`,
      occurredAt: `2026-08-10T10:00:01.${String(999_999 - index).padStart(6, "0")}Z`,
    })),
  };
  assert.deepEqual(
    sanitizeDecisionTracesPayload(Buffer.from(
      JSON.stringify(boundary),
      "utf8",
    )),
    boundary,
  );
});

test("rejects hostile, noncanonical, private, and oversized trace payloads", () => {
  const {
    sanitizeDecisionTracesPayload,
    sanitizeDecisionTracesValue,
  } = require("../gateway_proxy.js");
  const sanitize = (value) => sanitizeDecisionTracesPayload(
    Buffer.from(JSON.stringify(value), "utf8"),
  );
  const hostilePayloads = [];
  const mutate = (change) => {
    const value = structuredClone(safeDecisionTraces());
    change(value);
    hostilePayloads.push(value);
  };

  mutate((value) => { value.credential = "PRIVATE_CREDENTIAL"; });
  for (const field of [
    "accountId",
    "credential",
    "token",
    "header",
    "endpoint",
    "path",
    "prompt",
    "response",
    "rawError",
  ]) {
    mutate((value) => { value.traces[0][field] = `PRIVATE_${field}`; });
  }
  mutate((value) => { value.apiVersion = "gateway-decision-trace.openusage/v2"; });
  mutate((value) => { value.traces[0].traceId = "trace_ABCDEF0123456789abcdef0123456789"; });
  mutate((value) => { value.traces[0].occurredAt = "2026-08-10T10:00:03Z"; });
  mutate((value) => { value.traces[0].occurredAt = "2026-02-30T10:00:03.000000Z"; });
  mutate((value) => { value.traces.reverse(); });
  mutate((value) => { value.traces[1].traceId = value.traces[0].traceId; });
  mutate((value) => {
    value.traces = Array.from({ length: 129 }, (_item, index) => ({
      ...structuredClone(value.traces[2]),
      traceId: `trace_${index.toString(16).padStart(32, "0")}`,
      occurredAt: `2026-08-10T09:59:${String(59 - (index % 60)).padStart(2, "0")}.000000Z`,
    }));
  });
  mutate((value) => {
    value.traces[1].exclusions = Array.from({ length: 65 }, (_item, index) => ({
      accountDisplayId: `acct_${index.toString(16).padStart(12, "0")}`,
      reason: "cooldown",
    }));
  });
  mutate((value) => { value.traces[0].selected.providerId = "private-provider"; });
  mutate((value) => { value.traces[0].selected.accountDisplayId = "private-account-id"; });
  mutate((value) => { value.traces[0].fallback.attemptCount = 1; });
  mutate((value) => { value.traces[1].pool.revision = Number.MAX_SAFE_INTEGER + 1; });
  mutate((value) => { value.traces[1].exclusions[0].reason = "raw_provider_error"; });
  mutate((value) => { value.traces[2].execution = "executed"; });
  mutate((value) => { value.traces[2].selected = { providerId: "openai", accountDisplayId: null }; });
  mutate((value) => { value.traces[2].factsWindow.durationSeconds = 2_678_401; });

  for (const value of hostilePayloads) {
    assert.equal(sanitize(value), null, JSON.stringify(value));
  }

  const duplicateKey = JSON.stringify(safeDecisionTraces()).replace(
    '"apiVersion":"gateway-decision-trace.openusage/v1"',
    '"apiVersion":"gateway-decision-trace.openusage/v1","apiVersion":"gateway-decision-trace.openusage/v1"',
  );
  assert.equal(
    sanitizeDecisionTracesPayload(Buffer.from(duplicateKey, "utf8")),
    null,
  );
  const prototypeKey = JSON.stringify(safeDecisionTraces()).replace(
    '"traces":',
    '"__proto__":{"credential":"PRIVATE"},"traces":',
  );
  assert.equal(
    sanitizeDecisionTracesPayload(Buffer.from(prototypeKey, "utf8")),
    null,
  );

  const accessorPayload = safeDecisionTraces();
  Object.defineProperty(accessorPayload.traces[0], "selected", {
    enumerable: true,
    get() {
      throw new Error("PRIVATE_ACCESSOR_CANARY");
    },
  });
  assert.equal(sanitizeDecisionTracesValue(accessorPayload), null);

  const inheritedPayload = safeDecisionTraces();
  Object.setPrototypeOf(inheritedPayload.traces[0], {
    credential: "PRIVATE_INHERITED_CANARY",
  });
  assert.equal(sanitizeDecisionTracesValue(inheritedPayload), null);

  assert.equal(
    sanitizeDecisionTracesValue(new Proxy(safeDecisionTraces(), {})),
    null,
  );

  const prototypeBody = Buffer.from(
    JSON.stringify(safeDecisionTraces()),
    "utf8",
  );
  Object.setPrototypeOf(prototypeBody, { credential: "PRIVATE" });
  assert.equal(sanitizeDecisionTracesPayload(prototypeBody), null);
  assert.equal(
    sanitizeDecisionTracesPayload(new Proxy(
      Buffer.from(JSON.stringify(safeDecisionTraces()), "utf8"),
      { get: Reflect.get },
    )),
    null,
  );
  assert.equal(
    sanitizeDecisionTracesPayload(Buffer.alloc(1024 * 1024 + 1, 0x20)),
    null,
  );
});

test("bridges only sanitized Decision Traces through the authenticated loopback", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-decision-trace-")),
  );
  fs.chmodSync(root, 0o700);
  const token = "d".repeat(48);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, token, { encoding: "ascii", mode: 0o600 });
  const privateCanary = "PRIVATE_TRACE_HEADER_CANARY_7a1c";
  const upstreamRequests = [];
  const safe = safeDecisionTraces();
  const upstream = http.createServer((request, response) => {
    upstreamRequests.push({
      method: request.method,
      url: request.url,
      authorization: request.headers.authorization,
    });
    request.resume();
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("Set-Cookie", `${privateCanary}=1`);
    response.setHeader("X-Private-Path", `/private/${privateCanary}`);
    response.end(JSON.stringify(safe));
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

  const result = await fetch(
    `http://127.0.0.1:${rendererPort}/gateway/v1/decision-traces`,
  );
  const resultText = await result.text();
  assert.equal(result.status, 200);
  assert.deepEqual(JSON.parse(resultText), safe);
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.equal(result.headers.get("set-cookie"), null);
  assert.equal(result.headers.get("x-private-path"), null);
  assert.doesNotMatch(resultText, new RegExp(privateCanary));
  assert.deepEqual(upstreamRequests, [{
    method: "GET",
    url: "/gateway/v1/decision-traces",
    authorization: `Bearer ${token}`,
  }]);
});

test("collapses invalid upstream traces into one path-free 502", async (context) => {
  const {
    createRendererApiHandler,
    isRendererApiTarget,
  } = require("../gateway_proxy.js");
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "openusage-invalid-trace-")),
  );
  fs.chmodSync(root, 0o700);
  const tokenPath = path.join(root, "gateway.token");
  fs.writeFileSync(tokenPath, "i".repeat(48), {
    encoding: "ascii",
    mode: 0o600,
  });
  const privateCanary = "PRIVATE_RAW_PROVIDER_ERROR_26d9";
  const invalid = safeDecisionTraces();
  invalid.traces[0].rawError = `${privateCanary}:${tokenPath}`;
  const upstream = http.createServer((request, response) => {
    request.resume();
    response.end(JSON.stringify(invalid));
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

  const result = await fetch(
    `http://127.0.0.1:${rendererPort}/gateway/v1/decision-traces`,
  );
  const resultText = await result.text();
  assert.equal(result.status, 502);
  assert.deepEqual(JSON.parse(resultText), {
    error: { code: "service_unavailable" },
  });
  assert.doesNotMatch(resultText, /PRIVATE|credential|token|path|raw|provider/i);
});
