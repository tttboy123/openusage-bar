"use strict";

const path = require("path");
const http = require("http");
const { TextDecoder } = require("util");
const { readPrivateToken } = require("./gateway_proxy");

const PLUGIN_HOST = "127.0.0.1";
const PLUGIN_PORT = 17824;
const PLUGIN_CONNECTIONS_ROUTE = "/host/v1/plugin-connections";
const PRIVATE_PLUGIN_CONNECTIONS_ROUTE = "/plugin/v1/connections";
const MAX_PLUGIN_CONNECTIONS_RESPONSE_BYTES = 64 * 1024;
const MAX_RESPONSE_HEADER_BYTES = 16 * 1024;
const MAX_RESPONSE_HEADER_COUNT = 64;
const DEFAULT_DEADLINE_MS = 3_000;
const PLUGIN_API_VERSION = "plugin.openusage/v1";
const PLUGIN_OBJECT = "plugin.connections";
const PLUGIN_IDS = Object.freeze(["loom", "codex", "claude_code"]);
const CONFIGURATION_STATES = new Set([
  "configured",
  "not_configured",
  "unknown",
]);
const CONNECTION_STATES = new Set([
  "never_seen",
  "connected",
  "stale",
  "unknown",
]);
const CAPABILITY_STATES = new Set([
  "not_negotiated",
  "negotiated",
  "incompatible",
  "unknown",
]);
const PLUGIN_CAPABILITIES = new Set([
  "usage.query",
  "quotas.query",
  "health.query",
  "route.advice",
  "outcome.record",
  "decision.lookup",
]);
const NEGOTIATED_CAPABILITIES = Object.freeze(
  [...PLUGIN_CAPABILITIES].sort(),
);
const SYNC_OUTCOMES = new Set(["never", "succeeded", "failed", "unknown"]);
const FORBIDDEN_RENDERER_HEADERS = new Set([
  "authorization",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  "x-api-key",
  "x-auth-token",
  "content-length",
  "transfer-encoding",
  "expect",
]);

function discoverPrivatePluginRuntime({ platform, homeDir, localAppData } = {}) {
  try {
    if (platform === "win32") {
      const root = validateAbsoluteRoot(localAppData, path.win32, true);
      return Object.freeze({
        transport: "tcp",
        host: PLUGIN_HOST,
        port: PLUGIN_PORT,
        tokenPath: path.win32.join(
          root,
          "openusage-bar",
          "plugin",
          "desktop.token",
        ),
      });
    }
    if (
      platform === "darwin" ||
      (typeof platform === "string" && platform.startsWith("linux"))
    ) {
      const root = validateAbsoluteRoot(homeDir, path.posix, false);
      return Object.freeze({
        transport: "tcp",
        host: PLUGIN_HOST,
        port: PLUGIN_PORT,
        tokenPath: path.posix.join(
          root,
          ".local",
          "state",
          "openusage-bar",
          "plugin",
          "desktop.token",
        ),
      });
    }
  } catch {
    // Collapse path details at this private discovery boundary.
  }
  throw new Error("plugin runtime unavailable");
}

function validateAbsoluteRoot(value, pathApi, rejectUnc) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 4096 ||
    value.includes("\0") ||
    !pathApi.isAbsolute(value)
  ) {
    throw new Error("invalid runtime root");
  }
  const normalized = pathApi.normalize(value);
  if (normalized !== value && normalized !== `${value}${pathApi.sep}`) {
    throw new Error("invalid runtime root");
  }
  if (rejectUnc && value.startsWith("\\\\")) {
    throw new Error("invalid runtime root");
  }
  return value;
}

function isPluginConnectionRendererTarget(target) {
  return target === PLUGIN_CONNECTIONS_ROUTE;
}

function classifyPluginConnectionRequest(request) {
  try {
    if (!isPlainRecord(request)) return null;
    const values = exactOwnDataValues(request, ["method", "target", "headers"], [
      "method",
      "target",
    ]);
    if (values === null) return null;
    if (values.method !== "GET" || values.target !== PLUGIN_CONNECTIONS_ROUTE) {
      return null;
    }
    if (!safeRendererHeaders(values.headers)) return null;
    return Object.freeze({ method: "GET", target: PRIVATE_PLUGIN_CONNECTIONS_ROUTE });
  } catch {
    return null;
  }
}

function sanitizePluginConnectionsPayload(body) {
  try {
    if (!Buffer.isBuffer(body) || body.length > MAX_PLUGIN_CONNECTIONS_RESPONSE_BYTES) {
      return null;
    }
    const text = new TextDecoder("utf-8", { fatal: true }).decode(body);
    if (text.includes("\0")) {
      return null;
    }
    return sanitizePluginConnectionsValue(JSON.parse(text));
  } catch {
    return null;
  }
}

function sanitizePluginConnectionsValue(value) {
  try {
    const root = exactOwnDataValues(
      value,
      ["apiVersion", "object", "observedAt", "connections"],
      ["apiVersion", "object", "observedAt", "connections"],
    );
    if (
      root === null ||
      root.apiVersion !== PLUGIN_API_VERSION ||
      root.object !== PLUGIN_OBJECT ||
      !isCanonicalTimestamp(root.observedAt)
    ) {
      return null;
    }
    const connections = safeDenseArray(root.connections);
    if (connections === null || connections.length !== PLUGIN_IDS.length) {
      return null;
    }
    const sanitizedConnections = [];
    for (let index = 0; index < PLUGIN_IDS.length; index += 1) {
      const item = sanitizePluginConnection(connections[index], PLUGIN_IDS[index]);
      if (item === null) return null;
      sanitizedConnections.push(item);
    }
    return {
      apiVersion: PLUGIN_API_VERSION,
      object: PLUGIN_OBJECT,
      observedAt: root.observedAt,
      connections: sanitizedConnections,
    };
  } catch {
    return null;
  }
}

async function fetchPrivatePluginConnections({
  runtime,
  platform = process.platform,
  verifyWindowsAcl,
  deadlineMs = DEFAULT_DEADLINE_MS,
  requestImpl = http.request,
} = {}) {
  const unavailable = () => new Error("plugin connections unavailable");
  try {
    const endpoint = exactOwnDataValues(
      runtime,
      ["transport", "host", "port", "tokenPath"],
      ["transport", "host", "port", "tokenPath"],
    );
    if (
      endpoint === null ||
      endpoint.transport !== "tcp" ||
      endpoint.host !== PLUGIN_HOST ||
      endpoint.port !== PLUGIN_PORT ||
      typeof endpoint.tokenPath !== "string" ||
      typeof requestImpl !== "function"
    ) {
      throw unavailable();
    }
    const token = readPrivateToken(endpoint.tokenPath, {
      platform,
      verifyWindowsAcl,
    });
    const body = await boundedPluginRequest(
      {
        method: "GET",
        hostname: PLUGIN_HOST,
        port: PLUGIN_PORT,
        path: PRIVATE_PLUGIN_CONNECTIONS_ROUTE,
        agent: false,
        maxHeaderSize: MAX_RESPONSE_HEADER_BYTES,
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
          Host: `${PLUGIN_HOST}:${PLUGIN_PORT}`,
        },
      },
      { requestImpl, deadlineMs },
    );
    const sanitized = sanitizePluginConnectionsPayload(body);
    if (sanitized === null) throw unavailable();
    return sanitized;
  } catch {
    throw unavailable();
  }
}

function createPluginConnectionHandler({
  runtime,
  platform = process.platform,
  verifyWindowsAcl,
  deadlineMs = DEFAULT_DEADLINE_MS,
  fetchConnections = fetchPrivatePluginConnections,
} = {}) {
  if (typeof fetchConnections !== "function") {
    throw new Error("plugin connection handler unavailable");
  }
  return async function pluginConnectionHandler(request, response) {
    const classified = classifyPluginConnectionRequest({
      method: request?.method,
      target: request?.url,
      headers: distinctRequestHeaders(request),
    });
    if (classified === null) {
      sendJson(response, 404, { error: { code: "not_found" } });
      return;
    }
    try {
      const upstream = await fetchConnections({
        runtime,
        platform,
        verifyWindowsAcl,
        deadlineMs,
      });
      const sanitized = sanitizePluginConnectionsValue(upstream);
      if (sanitized === null) throw new Error("invalid plugin projection");
      sendJson(response, 200, sanitized);
    } catch {
      sendJson(response, 502, { error: { code: "service_unavailable" } });
    }
  };
}

function distinctRequestHeaders(request) {
  try {
    const source = request?.headersDistinct;
    if (source !== undefined) {
      if (!isPlainRecord(source)) return null;
      const result = {};
      for (const [name, values] of Object.entries(source)) {
        if (!Array.isArray(values) || values.length !== 1) return null;
        result[name] = values[0];
      }
      return result;
    }
    return isPlainRecord(request?.headers) ? request.headers : null;
  } catch {
    return null;
  }
}

function sendJson(response, statusCode, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Length": String(body.length),
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

function boundedPluginRequest(options, { requestImpl, deadlineMs }) {
  return new Promise((resolve, reject) => {
    const unavailable = () => new Error("plugin connections unavailable");
    let settled = false;
    let timer;
    let request;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    try {
      request = requestImpl(options, (response) => {
        if (
          !boundedRawHeaders(response?.rawHeaders) ||
          response.statusCode !== 200 ||
          !isJsonContentType(response.headers?.["content-type"]) ||
          !boundedDeclaredLength(response.headers?.["content-length"])
        ) {
          finish(reject, unavailable());
          response?.destroy?.();
          request?.destroy?.();
          return;
        }
        const chunks = [];
        let total = 0;
        response.on("data", (chunk) => {
          if (!Buffer.isBuffer(chunk)) chunk = Buffer.from(chunk);
          total += chunk.length;
          if (total > MAX_PLUGIN_CONNECTIONS_RESPONSE_BYTES) {
            finish(reject, unavailable());
            response.destroy?.();
            request.destroy?.();
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          finish(resolve, Buffer.concat(chunks, total));
        });
        response.on("aborted", () => finish(reject, unavailable()));
        response.on("error", () => finish(reject, unavailable()));
      });
      if (!request || typeof request.on !== "function" || typeof request.end !== "function") {
        throw unavailable();
      }
      request.on("error", () => finish(reject, unavailable()));
      timer = setTimeout(() => {
        finish(reject, unavailable());
        request.destroy?.();
      }, validDeadline(deadlineMs));
      timer.unref?.();
      request.end();
    } catch {
      request?.destroy?.();
      finish(reject, unavailable());
    }
  });
}

function boundedRawHeaders(rawHeaders) {
  if (!Array.isArray(rawHeaders) || rawHeaders.length % 2 !== 0) return false;
  if (rawHeaders.length / 2 > MAX_RESPONSE_HEADER_COUNT) return false;
  let bytes = 0;
  for (const value of rawHeaders) {
    if (typeof value !== "string" || /[\r\n\0\x7f]/u.test(value)) return false;
    bytes += Buffer.byteLength(value, "utf8");
    if (bytes > MAX_RESPONSE_HEADER_BYTES) return false;
  }
  return true;
}

function isJsonContentType(value) {
  return value === "application/json" || value === "application/json; charset=utf-8";
}

function boundedDeclaredLength(value) {
  if (value === undefined) return true;
  if (Array.isArray(value) || typeof value !== "string" || !/^\d{1,5}$/.test(value)) {
    return false;
  }
  const length = Number(value);
  return length >= 0 && length <= MAX_PLUGIN_CONNECTIONS_RESPONSE_BYTES;
}

function validDeadline(value) {
  return Number.isInteger(value) && value >= 50 && value <= 30_000
    ? value
    : DEFAULT_DEADLINE_MS;
}

function sanitizePluginConnection(value, expectedPluginId) {
  const fields = [
    "pluginId",
    "configuration",
    "connection",
    "capabilityState",
    "capabilities",
    "lastSeenAt",
    "lastSyncOutcome",
    "lastSyncAt",
  ];
  const item = exactOwnDataValues(value, fields, fields);
  if (
    item === null ||
    item.pluginId !== expectedPluginId ||
    !CONFIGURATION_STATES.has(item.configuration) ||
    !CONNECTION_STATES.has(item.connection) ||
    !CAPABILITY_STATES.has(item.capabilityState) ||
    !SYNC_OUTCOMES.has(item.lastSyncOutcome) ||
    !isNullableTimestamp(item.lastSeenAt) ||
    !isNullableTimestamp(item.lastSyncAt)
  ) {
    return null;
  }

  const capabilities = safeDenseArray(item.capabilities);
  if (capabilities === null || capabilities.length > PLUGIN_CAPABILITIES.size) {
    return null;
  }
  let prior = null;
  for (const capability of capabilities) {
    if (
      typeof capability !== "string" ||
      !PLUGIN_CAPABILITIES.has(capability) ||
      (prior !== null && capability <= prior)
    ) {
      return null;
    }
    prior = capability;
  }
  if (item.capabilityState !== "negotiated" && capabilities.length !== 0) {
    return null;
  }
  if (
    item.capabilityState === "negotiated" &&
    (capabilities.length !== NEGOTIATED_CAPABILITIES.length ||
      capabilities.some(
        (capability, index) => capability !== NEGOTIATED_CAPABILITIES[index],
      ))
  ) {
    return null;
  }

  if (
    item.connection === "connected" &&
    (item.configuration !== "configured" || item.lastSeenAt === null)
  ) {
    return null;
  }
  if (item.connection === "never_seen" && item.lastSeenAt !== null) return null;
  if (
    item.configuration === "not_configured" &&
    (item.connection !== "never_seen" ||
      item.capabilityState !== "not_negotiated" ||
      capabilities.length !== 0 ||
      item.lastSeenAt !== null ||
      item.lastSyncOutcome !== "never" ||
      item.lastSyncAt !== null)
  ) {
    return null;
  }
  if (
    item.connection === "stale" &&
    (item.configuration !== "configured" || item.lastSeenAt === null)
  ) {
    return null;
  }
  if (
    (item.lastSyncOutcome === "succeeded" || item.lastSyncOutcome === "failed") !==
    (item.lastSyncAt !== null)
  ) {
    return null;
  }

  return {
    pluginId: expectedPluginId,
    configuration: item.configuration,
    connection: item.connection,
    capabilityState: item.capabilityState,
    capabilities: [...capabilities],
    lastSeenAt: item.lastSeenAt,
    lastSyncOutcome: item.lastSyncOutcome,
    lastSyncAt: item.lastSyncAt,
  };
}

function isNullableTimestamp(value) {
  return value === null || isCanonicalTimestamp(value);
}

function isCanonicalTimestamp(value) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{6}Z$/.test(value)
  ) {
    return false;
  }
  const millisecondTimestamp = `${value.slice(0, 23)}Z`;
  const parsed = new Date(millisecondTimestamp);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString() === millisecondTimestamp;
}

function safeDenseArray(value) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) {
    return null;
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.some((key) => typeof key !== "string")) return null;
  if (
    !Object.prototype.hasOwnProperty.call(descriptors, "length") ||
    !("value" in descriptors.length) ||
    descriptors.length.value !== value.length
  ) {
    return null;
  }
  const result = [];
  for (let index = 0; index < value.length; index += 1) {
    const key = String(index);
    const descriptor = descriptors[key];
    if (!descriptor || !("value" in descriptor) || !descriptor.enumerable) {
      return null;
    }
    result.push(descriptor.value);
  }
  if (ownKeys.length !== value.length + 1) return null;
  return result;
}

function safeRendererHeaders(headers) {
  if (headers === undefined) return true;
  if (!isPlainRecord(headers)) return false;
  const descriptors = Object.getOwnPropertyDescriptors(headers);
  if (Reflect.ownKeys(descriptors).some((key) => typeof key !== "string")) {
    return false;
  }
  for (const [rawName, descriptor] of Object.entries(descriptors)) {
    if (!("value" in descriptor) || !descriptor.enumerable) return false;
    const name = rawName.toLowerCase();
    if (FORBIDDEN_RENDERER_HEADERS.has(name)) return false;
    if (!/^[a-z0-9-]{1,128}$/.test(name)) return false;
    if (
      typeof descriptor.value !== "string" ||
      descriptor.value.length > 8192 ||
      /[\r\n\0]/.test(descriptor.value)
    ) {
      return false;
    }
  }
  return true;
}

function exactOwnDataValues(value, allowedKeys, requiredKeys = allowedKeys) {
  if (!isPlainRecord(value)) return null;
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const ownKeys = Reflect.ownKeys(descriptors);
  if (ownKeys.some((key) => typeof key !== "string")) return null;
  const keys = ownKeys;
  if (keys.some((key) => !allowedKeys.includes(key))) return null;
  for (const key of requiredKeys) {
    if (!Object.prototype.hasOwnProperty.call(descriptors, key)) return null;
  }
  const result = Object.create(null);
  for (const key of keys) {
    const descriptor = descriptors[key];
    if (!("value" in descriptor) || !descriptor.enumerable) return null;
    result[key] = descriptor.value;
  }
  return result;
}

function isPlainRecord(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

module.exports = {
  PLUGIN_CONNECTIONS_ROUTE,
  classifyPluginConnectionRequest,
  createPluginConnectionHandler,
  discoverPrivatePluginRuntime,
  fetchPrivatePluginConnections,
  isPluginConnectionRendererTarget,
  sanitizePluginConnectionsPayload,
  sanitizePluginConnectionsValue,
};
