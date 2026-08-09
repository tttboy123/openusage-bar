import assert from "node:assert/strict";
import test from "node:test";
import { normalizeRuntimeCapability } from "../.test-dist/runtimeCapability.js";


const API_VERSION = "runtime-capability.openusage/v1";
const OBJECT_TYPE = "runtime.capability";
const SERIALIZED_UTF8_LIMIT = 64 * 1024;
const FEATURE_IDS = [
  "listener",
  "should_send",
  "responses",
  "cache",
  "fallback",
  "pii_redaction",
  "streaming",
];
const ACTION_IDS = [
  "enable",
  "disable",
  "open_settings",
  "retry",
  "learn_more",
  "clear_cache",
];
const PRIVATE_CANARIES = {
  host: "CANARY_HOST_3b01",
  port: "CANARY_PORT_3b02",
  url: "CANARY_URL_3b03",
  socketPath: "CANARY_SOCKET_3b04",
  namedPipe: "CANARY_PIPE_3b05",
  tokenPath: "CANARY_TOKEN_PATH_3b06",
  databasePath: "CANARY_DATABASE_3b07",
  homePath: "CANARY_HOME_3b08",
  bearer: "CANARY_BEARER_3b09",
  authorization: "CANARY_AUTHORIZATION_3b10",
  cookie: "CANARY_COOKIE_3b11",
  apiKey: "CANARY_API_KEY_3b12",
  providerHeaders: "CANARY_PROVIDER_HEADERS_3b13",
  credentials: "CANARY_CREDENTIALS_3b14",
  accountRef: "CANARY_ACCOUNT_3b15",
  providerRequestId: "CANARY_PROVIDER_REQUEST_3b16",
  controlId: "CANARY_CONTROL_3b17",
  toolCallId: "CANARY_TOOL_CALL_3b18",
  cacheKey: "CANARY_CACHE_KEY_3b19",
  prompt: "CANARY_PROMPT_3b20",
  requestBody: "CANARY_REQUEST_BODY_3b21",
  responseBody: "CANARY_RESPONSE_BODY_3b22",
  outputText: "CANARY_OUTPUT_3b23",
  toolOutput: "CANARY_TOOL_OUTPUT_3b24",
  rawChunk: "CANARY_RAW_CHUNK_3b25",
  delta: "CANARY_DELTA_3b26",
  rawError: "CANARY_RAW_ERROR_3b27",
  stack: "CANARY_STACK_3b28",
  command: "CANARY_COMMAND_3b29",
  arguments: "CANARY_ARGUMENTS_3b30",
  ipcChannel: "CANARY_IPC_3b31",
};


test("normalizes the Python runtime capability into the closed safe view model", () => {
  assert.deepEqual(normalizeRuntimeCapability(wireEnvelope()), safeEnvelope());
  assert.equal(normalizeRuntimeCapability(null), null);
  assert.equal(
    normalizeRuntimeCapability({ ...wireEnvelope(), apiVersion: "future/v9" }),
    null,
  );
  assert.equal(
    normalizeRuntimeCapability({ ...wireEnvelope(), object: "runtime.private" }),
    null,
  );
});


test("keeps schemaVersion within the shared 64-character contract", () => {
  const atLimit = `openusage/v${"1".repeat(53)}`;
  const overLimit = `${atLimit}1`;
  assert.equal(atLimit.length, 64);
  assert.equal(overLimit.length, 65);
  assert.match(atLimit, /^(?:openusage\/v\d+|\d+\.\d+)$/);
  assert.match(overLimit, /^(?:openusage\/v\d+|\d+\.\d+)$/);

  const accepted = wireEnvelope();
  accepted.observer.schemaVersion = atLimit;
  assert.equal(
    normalizeRuntimeCapability(accepted).observer.schemaVersion,
    atLimit,
  );

  const rejected = wireEnvelope();
  rejected.observer.schemaVersion = overLimit;
  assert.equal(normalizeRuntimeCapability(rejected).observer.schemaVersion, null);
});


test("retains the generic 256-character text budget outside schemaVersion", () => {
  const atLimit = wireEnvelope();
  atLimit.futureText = "x".repeat(256);
  assert.deepEqual(normalizeRuntimeCapability(atLimit), safeEnvelope());

  const overLimit = wireEnvelope();
  overLimit.futureText = "x".repeat(257);
  assert.equal(normalizeRuntimeCapability(overLimit), null);
});


test("counts bounded astral text as Unicode code points", () => {
  const atLimit = wireEnvelope();
  atLimit.futureText = "\u{1F680}".repeat(256);
  const accepted = normalizeRuntimeCapability(atLimit);
  assert.deepEqual(accepted, safeEnvelope());
  assert.equal(JSON.stringify(accepted).includes("\u{1F680}"), false);

  const overLimit = wireEnvelope();
  overLimit.futureText = "\u{1F680}".repeat(257);
  assert.equal(normalizeRuntimeCapability(overLimit), null);
});


test("rejects lone surrogate text without reflecting it", () => {
  for (const surrogate of ["\uD800", "\uDC00"]) {
    const input = wireEnvelope();
    input.futureText = surrogate;
    const normalized = normalizeRuntimeCapability(input);
    assert.equal(normalized, null);
    assert.equal(JSON.stringify(normalized).includes(surrogate), false);
  }
});


test("preserves year 0001 timestamps across Observer and Gateway", () => {
  const yearOne = "0001-01-01T00:00:00Z";
  const input = wireEnvelope();
  input.observer.generatedAt = yearOne;
  input.observer.lastGoodAt = yearOne;
  input.gateway.lastError.observedAt = yearOne;
  const expected = safeEnvelope();
  expected.observer.generatedAt = yearOne;
  expected.observer.lastGoodAt = yearOne;
  expected.gateway.lastError.observedAt = yearOne;
  assert.deepEqual(normalizeRuntimeCapability(input), expected);
});


test("rejects year 0000 timestamps without crossing capability boundaries", () => {
  const yearZero = "0000-01-01T00:00:00Z";
  const input = wireEnvelope();
  input.observer.generatedAt = yearZero;
  input.observer.lastGoodAt = yearZero;
  input.gateway.lastError.observedAt = yearZero;
  const expected = safeEnvelope();
  expected.observer.generatedAt = null;
  expected.observer.lastGoodAt = null;
  expected.gateway.lastError = {
    code: "capability_invalid",
    retryable: false,
  };
  assert.deepEqual(normalizeRuntimeCapability(input), expected);
});


test("rebuilds closed objects and degrades an unknown mode without reflection", () => {
  const additive = wireEnvelope();
  additive.futureTop = "DROP_TOP";
  additive.observer.futureObserver = "DROP_OBSERVER";
  additive.gateway.futureGateway = "DROP_GATEWAY";
  additive.gateway.features.listener.futureFeature = "DROP_FEATURE";
  additive.gateway.lastError.futureError = "DROP_ERROR";
  assert.deepEqual(normalizeRuntimeCapability(additive), safeEnvelope());

  const unknownMode = wireEnvelope();
  unknownMode.gateway.mode = "future-mode-secret";
  unknownMode.gateway.operational = "unknown";
  unknownMode.gateway.features = unknownFeatures();
  unknownMode.gateway.configuredProviderCount = null;
  unknownMode.gateway.healthyProviderCount = null;
  unknownMode.gateway.actions = ["learn_more"];
  unknownMode.gateway.lastError = null;
  const expected = safeEnvelope();
  expected.gateway = {
    mode: "unknown",
    operational: "unknown",
    features: unknownFeatures(),
    configuredProviderCount: null,
    healthyProviderCount: null,
    actions: ["learn_more"],
    lastError: null,
  };
  const normalized = normalizeRuntimeCapability(unknownMode);
  assert.deepEqual(normalized, expected);
  assert.doesNotMatch(JSON.stringify(normalized), /future-mode-secret|DROP_/);
});


test("unsupported support suppresses enabled, configured, and operational claims", () => {
  const safeUnsupported = {
    support: "unsupported",
    enabled: "unknown",
    configured: "unknown",
    operational: "unknown",
  };
  for (const operational of ["starting", "degraded", "unavailable"]) {
    const input = wireEnvelope();
    input.gateway.features.listener = feature({
      support: "unsupported",
      enabled: true,
      configured: true,
      operational,
    });
    const expected = safeEnvelope();
    expected.gateway.features.listener = safeUnsupported;
    assert.deepEqual(normalizeRuntimeCapability(input), expected, operational);
  }
});


test("observe mode accepts the explicitly disabled listener state", () => {
  const input = observeEnvelope();
  const expected = safeEnvelope();
  expected.gateway.mode = "observe";
  expected.gateway.features.listener = feature({
    enabled: false,
    configured: false,
    operational: "disabled",
  });
  assert.deepEqual(normalizeRuntimeCapability(input), expected);
});


test("observe mode preserves an unsupported disabled listener idempotently", () => {
  const input = observeEnvelope({ support: "unsupported" });
  const expected = safeEnvelope();
  expected.gateway.mode = "observe";
  expected.gateway.features.listener = {
    support: "unsupported",
    enabled: false,
    configured: false,
    operational: "disabled",
  };

  const normalized = normalizeRuntimeCapability(input);
  assert.deepEqual(normalized, expected);
  assert.deepEqual(normalizeRuntimeCapability(normalized), normalized);
});


const OBSERVE_LISTENER_CONTRADICTIONS = [
  ["enabled true", { enabled: true }],
  ["enabled unknown", { enabled: "unknown" }],
  ["configured true", { configured: true }],
  ["configured unknown", { configured: "unknown" }],
  ["operational starting", { operational: "starting" }],
  ["operational ready", { operational: "ready" }],
  ["operational degraded", { operational: "degraded" }],
  ["operational unavailable", { operational: "unavailable" }],
  ["operational unknown", { operational: "unknown" }],
];

for (const [name, override] of OBSERVE_LISTENER_CONTRADICTIONS) {
  test(`observe mode rejects listener ${name} without losing Observer`, () => {
    const normalized = normalizeRuntimeCapability(observeEnvelope(override));
    assert.deepEqual(normalized.observer, observer());
    assert.deepEqual(normalized.gateway, unknownGateway());
  });
}


test("malformed or contradictory Gateway facts fail closed without losing Observer", () => {
  const unknownFeatureEnum = wireGateway();
  unknownFeatureEnum.features.listener.operational = "future-ready";
  const negativeCount = wireGateway();
  negativeCount.configuredProviderCount = -1;
  const nonIntegerCount = wireGateway();
  nonIntegerCount.healthyProviderCount = "1";
  const impossibleCounts = wireGateway();
  impossibleCounts.healthyProviderCount = 3;
  const contradictory = wireGateway();
  contradictory.mode = "observe";
  contradictory.features.listener = feature({
    enabled: false,
    configured: false,
    operational: "ready",
  });

  const cases = [
    null,
    { mode: "gateway", features: [] },
    unknownFeatureEnum,
    negativeCount,
    nonIntegerCount,
    impossibleCounts,
    contradictory,
  ];
  for (const gateway of cases) {
    const input = wireEnvelope();
    input.gateway = gateway;
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(normalized.observer, observer());
    assert.deepEqual(normalized.gateway, unknownGateway());
  }
});


test("keeps Provider counts and streaming as explicit independent facts", () => {
  const input = wireEnvelope();
  delete input.gateway.configuredProviderCount;
  delete input.gateway.healthyProviderCount;
  input.gateway.adapterCount = 5;
  input.gateway.adapters = ["openai", "anthropic", "gemini", "ollama", "codex"];
  input.gateway.dispatchEvents = true;
  input.gateway.features.listener.operational = "unknown";
  input.gateway.features.streaming.operational = "degraded";

  const normalized = normalizeRuntimeCapability(input);
  assert.equal(normalized.gateway.configuredProviderCount, null);
  assert.equal(normalized.gateway.healthyProviderCount, null);
  assert.equal(normalized.gateway.features.listener.operational, "unknown");
  assert.equal(normalized.gateway.features.streaming.operational, "degraded");
  assert.equal("adapterCount" in normalized.gateway, false);
  assert.equal("adapters" in normalized.gateway, false);
  assert.equal("dispatchEvents" in normalized.gateway, false);

  const maximum = wireEnvelope();
  maximum.observer.dataRevision = Number.MAX_SAFE_INTEGER;
  maximum.gateway.configuredProviderCount = Number.MAX_SAFE_INTEGER;
  maximum.gateway.healthyProviderCount = null;
  const maximumNormalized = normalizeRuntimeCapability(maximum);
  assert.equal(maximumNormalized.observer.dataRevision, Number.MAX_SAFE_INTEGER);
  assert.equal(
    maximumNormalized.gateway.configuredProviderCount,
    Number.MAX_SAFE_INTEGER,
  );

  const oversizedRevision = wireEnvelope();
  oversizedRevision.observer.dataRevision = Number.MAX_SAFE_INTEGER + 1;
  assert.equal(
    normalizeRuntimeCapability(oversizedRevision).observer.dataRevision,
    null,
  );
  for (const field of ["configuredProviderCount", "healthyProviderCount"]) {
    const oversizedCount = wireEnvelope();
    oversizedCount.gateway.configuredProviderCount = null;
    oversizedCount.gateway.healthyProviderCount = null;
    oversizedCount.gateway[field] = Number.MAX_SAFE_INTEGER + 1;
    assert.deepEqual(
      normalizeRuntimeCapability(oversizedCount).gateway,
      unknownGateway(),
      field,
    );
  }
});


test("normalizes only advertised allowlisted actions in canonical order", () => {
  const subset = wireEnvelope();
  subset.gateway.actions = [
    "learn_more",
    "retry",
    "unknown_action",
    "open_settings",
    "retry",
  ];
  assert.deepEqual(
    normalizeRuntimeCapability(subset).gateway.actions,
    ["open_settings", "retry", "learn_more"],
  );

  const empty = wireEnvelope();
  empty.gateway.actions = [];
  assert.deepEqual(normalizeRuntimeCapability(empty).gateway.actions, []);

  const unknownError = wireEnvelope();
  unknownError.gateway.lastError = {
    code: "PRIVATE_UPSTREAM_CODE_88",
    retryable: true,
    message: "PRIVATE_UPSTREAM_MESSAGE_89",
  };
  const normalized = normalizeRuntimeCapability(unknownError);
  assert.deepEqual(normalized.gateway.lastError, {
    code: "capability_invalid",
    retryable: false,
  });
  assert.doesNotMatch(JSON.stringify(normalized), /PRIVATE_UPSTREAM_/);
});


test("drops every private canary from normalized output and console", () => {
  const input = wireEnvelope();
  Object.assign(input, PRIVATE_CANARIES);
  Object.assign(input.observer, PRIVATE_CANARIES);
  Object.assign(input.gateway, PRIVATE_CANARIES);
  Object.assign(input.gateway.features.listener, PRIVATE_CANARIES);
  Object.assign(input.gateway.lastError, PRIVATE_CANARIES);

  const calls = [];
  const methods = ["debug", "error", "info", "log", "warn"];
  const originals = Object.fromEntries(methods.map((name) => [name, console[name]]));
  let normalized;
  try {
    for (const name of methods) {
      console[name] = (...args) => calls.push([name, ...args]);
    }
    normalized = normalizeRuntimeCapability(input);
  } finally {
    for (const name of methods) {
      console[name] = originals[name];
    }
  }

  const serialized = JSON.stringify({ normalized, calls });
  for (const canary of Object.values(PRIVATE_CANARIES)) {
    assert.equal(serialized.includes(canary), false, canary);
  }
});


test("hostile Gateway Proxy and accessors become an unknown Gateway", () => {
  const expected = {
    apiVersion: API_VERSION,
    object: OBJECT_TYPE,
    observer: observer(),
    gateway: unknownGateway(),
  };
  const hostileGateways = [
    ["proxy", "CANARY_GATEWAY_PROXY_7c02", () => {
      const input = wireEnvelope();
      input.gateway = new Proxy(wireGateway(), {
        get(target, property, receiver) {
          if (property === "mode") {
            throw new Error("CANARY_GATEWAY_PROXY_7c02");
          }
          return Reflect.get(target, property, receiver);
        },
      });
      return input;
    }],
    ["accessor", "CANARY_GATEWAY_ACCESSOR_7c03", () => {
      const input = wireEnvelope();
      Object.defineProperty(input.gateway, "features", {
        enumerable: true,
        get() {
          throw new Error("CANARY_GATEWAY_ACCESSOR_7c03");
        },
      });
      return input;
    }],
  ];

  for (const [name, secret, makeInput] of hostileGateways) {
    const { result, calls } = captureConsole(() =>
      normalizeRuntimeCapability(makeInput()),
    );
    assert.deepEqual(result, expected, name);
    assert.deepEqual(calls, [], name);
    assert.equal(JSON.stringify(result).includes(secret), false, name);
  }
});


test("a throwing top-level Gateway getter is isolated from Observer", () => {
  const secret = "CANARY_THROWING_GATEWAY_GETTER_7c01";
  const input = wireEnvelope();
  Object.defineProperty(input, "gateway", {
    enumerable: true,
    get() {
      throw new Error(secret);
    },
  });
  const { result, calls } = captureConsole(() =>
    normalizeRuntimeCapability(input),
  );
  assert.deepEqual(calls, []);
  assert.equal(JSON.stringify(result).includes(secret), false);
  assert.deepEqual(result, {
    apiVersion: API_VERSION,
    object: OBJECT_TYPE,
    observer: observer(),
    gateway: unknownGateway(),
  });
});


test("throwing envelope identity getters return null without logging", () => {
  for (const field of ["apiVersion", "object"]) {
    const secret = `CANARY_THROWING_${field}_7c04`;
    const input = wireEnvelope();
    Object.defineProperty(input, field, {
      enumerable: true,
      get() {
        throw new Error(secret);
      },
    });
    const { result, calls } = captureConsole(() =>
      normalizeRuntimeCapability(input),
    );
    assert.equal(result, null, field);
    assert.deepEqual(calls, [], field);
  }
});


for (const field of ["apiVersion", "object", "observer", "gateway"]) {
  test(`does not accept inherited root ${field} as an own contract fact`, () => {
    assert.equal(
      normalizeRuntimeCapability(inheritOneField(wireEnvelope(), field)),
      null,
    );
  });
}


test("accepts valid own JSON facts on ordinary and null-prototype objects", () => {
  for (const input of [wireEnvelope(), nullPrototypeJson(wireEnvelope())]) {
    assert.deepEqual(normalizeRuntimeCapability(input), safeEnvelope());
  }
});


for (const featureId of FEATURE_IDS) {
  test(`does not accept inherited ${featureId} as a feature entry`, () => {
    const input = wireEnvelope();
    input.gateway.features = inheritOneField(
      input.gateway.features,
      featureId,
    );
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(normalized.observer, observer());
    assert.deepEqual(normalized.gateway, unknownGateway());
  });
}


for (const field of ["support", "enabled", "configured", "operational"]) {
  test(`does not accept inherited listener ${field} as a feature fact`, () => {
    const input = wireEnvelope();
    input.gateway.features.listener = inheritOneField(
      input.gateway.features.listener,
      field,
    );
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(normalized.observer, observer());
    assert.deepEqual(normalized.gateway, unknownGateway());
  });
}


test("normalizes darwin, win32, and linux fixtures to identical semantics", () => {
  const snapshots = {};
  for (const platform of ["darwin", "win32", "linux"]) {
    const input = wireEnvelope();
    input.platform = platform;
    input.gateway.platform = platform;
    input.gateway.transport = `CANARY_TRANSPORT_${platform}`;
    input.gateway.runtimePath = `CANARY_RUNTIME_PATH_${platform}`;
    snapshots[platform] = normalizeRuntimeCapability(input);
  }

  assert.deepEqual(snapshots.darwin, snapshots.win32);
  assert.deepEqual(snapshots.darwin, snapshots.linux);
  const serialized = JSON.stringify(Object.values(snapshots));
  for (const forbidden of [
    "platform",
    "darwin",
    "win32",
    "linux",
    "CANARY_TRANSPORT_",
    "CANARY_RUNTIME_PATH_",
  ]) {
    assert.equal(serialized.includes(forbidden), false, forbidden);
  }
});


test("deep or oversized Gateway input becomes a bounded unknown Gateway", () => {
  const deep = wireGateway();
  deep.future = deepValue(24);
  const oversized = wireGateway();
  oversized.future = "界".repeat(22_000);
  assert.ok(serializedUtf8Size(oversized) > SERIALIZED_UTF8_LIMIT);

  for (const gateway of [deep, oversized]) {
    const input = wireEnvelope();
    input.gateway = gateway;
    const normalized = normalizeRuntimeCapability(input);
    assert.deepEqual(normalized.observer, observer());
    assert.deepEqual(normalized.gateway, unknownGateway());
    assert.ok(serializedUtf8Size(normalized) <= SERIALIZED_UTF8_LIMIT);
  }
});


function feature(overrides = {}) {
  return {
    support: "supported",
    enabled: true,
    configured: true,
    operational: "ready",
    ...overrides,
  };
}


function unknownFeature() {
  return feature({
    support: "unknown",
    enabled: "unknown",
    configured: "unknown",
    operational: "unknown",
  });
}


function observer() {
  return {
    operational: "ready",
    generatedAt: "2026-08-08T15:00:00Z",
    lastGoodAt: "2026-08-08T14:59:00Z",
    dataRevision: 42,
    schemaVersion: "openusage/v1",
  };
}


function features() {
  return {
    listener: feature(),
    should_send: feature({ operational: "starting" }),
    responses: feature({ configured: false, operational: "starting" }),
    cache: feature({ enabled: false, operational: "disabled" }),
    fallback: unknownFeature(),
    pii_redaction: feature({ operational: "degraded" }),
    streaming: feature({ operational: "degraded" }),
  };
}


function unknownFeatures() {
  return Object.fromEntries(
    Object.keys(features()).map((featureId) => [featureId, unknownFeature()]),
  );
}


function safeGateway() {
  return {
    mode: "gateway",
    operational: "degraded",
    features: features(),
    configuredProviderCount: 2,
    healthyProviderCount: 1,
    actions: [...ACTION_IDS],
    lastError: {
      code: "provider_unavailable",
      retryable: true,
      observedAt: "2026-08-08T14:58:00Z",
    },
  };
}


function wireGateway() {
  return {
    ...safeGateway(),
    actions: [
      "clear_cache",
      "learn_more",
      "retry",
      "open_settings",
      "disable",
      "enable",
      "retry",
      "unknown_action",
    ],
    adapterCount: 5,
    dispatchEvents: true,
  };
}


function unknownGateway() {
  return {
    mode: "unknown",
    operational: "unknown",
    features: unknownFeatures(),
    configuredProviderCount: null,
    healthyProviderCount: null,
    actions: ["retry"],
    lastError: {
      code: "capability_invalid",
      retryable: false,
    },
  };
}


function safeEnvelope() {
  return {
    apiVersion: API_VERSION,
    object: OBJECT_TYPE,
    observer: observer(),
    gateway: safeGateway(),
  };
}


function wireEnvelope() {
  return {
    apiVersion: API_VERSION,
    object: OBJECT_TYPE,
    observer: observer(),
    gateway: wireGateway(),
  };
}


function observeEnvelope(listenerOverrides = {}) {
  const input = wireEnvelope();
  input.gateway.mode = "observe";
  input.gateway.features.listener = feature({
    enabled: false,
    configured: false,
    operational: "disabled",
    ...listenerOverrides,
  });
  return input;
}


function inheritOneField(source, field) {
  const own = { ...source };
  delete own[field];
  return Object.assign(Object.create({ [field]: source[field] }), own);
}


function nullPrototypeJson(value) {
  if (Array.isArray(value)) return value.map(nullPrototypeJson);
  if (value !== null && typeof value === "object") {
    const result = Object.create(null);
    for (const [key, item] of Object.entries(value)) {
      result[key] = nullPrototypeJson(item);
    }
    return result;
  }
  return value;
}


function deepValue(depth) {
  let value = "leaf";
  for (let index = 0; index < depth; index += 1) {
    value = { nested: value };
  }
  return value;
}


function serializedUtf8Size(value) {
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}


function captureConsole(callback) {
  const methods = ["debug", "error", "info", "log", "warn"];
  const originals = Object.fromEntries(methods.map((name) => [name, console[name]]));
  const calls = [];
  try {
    for (const name of methods) {
      console[name] = (...args) => calls.push([name, ...args]);
    }
    return { result: callback(), calls };
  } finally {
    for (const name of methods) {
      console[name] = originals[name];
    }
  }
}
