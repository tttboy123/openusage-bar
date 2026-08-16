"use strict";

const API_VERSION = "runtime-capability.openusage/v1";
const OBJECT_TYPE = "runtime.capability";
const SERIALIZED_UTF8_LIMIT = 64 * 1024;
const MAX_TEXT_LENGTH = 256;
const SCHEMA_VERSION_MAX_LENGTH = 64;
const MAX_INPUT_DEPTH = 16;
const MAX_INPUT_NODES = 4096;

const SUPPORT_VALUES = new Set(["supported", "unsupported", "unknown"]);
const OPERATIONAL_VALUES = new Set([
  "disabled",
  "starting",
  "ready",
  "degraded",
  "unavailable",
  "unknown",
]);
const GATEWAY_MODES = new Set(["observe", "advise", "gateway", "unknown"]);
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
const SAFE_ERROR_CODES = new Set([
  "gateway_disabled",
  "gateway_starting",
  "gateway_timeout",
  "gateway_unavailable",
  "provider_unavailable",
  "credential_backend_unavailable",
  "capability_invalid",
]);
const ROOT_FIELDS = new Set(["apiVersion", "object", "observer", "gateway"]);
const TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?Z$/u;
const SCHEMA_VERSION = /^(?:openusage\/v\d+|\d+\.\d+)$/u;
const INVALID = Symbol("invalid-runtime-capability-value");

function normalizeRuntimeCapability(value) {
  try {
    if (!isRecord(value)) return null;
    const apiVersion = readOwnData(value, "apiVersion");
    const objectType = readOwnData(value, "object");
    if (
      !apiVersion.ok ||
      apiVersion.value !== API_VERSION ||
      !objectType.ok ||
      objectType.value !== OBJECT_TYPE ||
      !hasOwn(value, "observer") ||
      !hasOwn(value, "gateway")
    ) {
      return null;
    }
    const additive = {};
    for (const key of Object.keys(value)) {
      if (!ROOT_FIELDS.has(key)) {
        const item = readOwnData(value, key);
        if (!item.ok) return null;
        additive[key] = item.value;
      }
    }
    if (!isBoundedJson(additive)) return null;

    const observer = readOwnData(value, "observer");
    const gateway = readOwnData(value, "gateway");
    return {
      apiVersion: API_VERSION,
      object: OBJECT_TYPE,
      observer: observer.ok ? normalizeObserver(observer.value) : unknownObserver(),
      gateway: gateway.ok ? normalizeGateway(gateway.value) : unknownGateway(),
    };
  } catch {
    return null;
  }
}

function composeRendererCapability({
  gatewayPayload,
  localHealthPayload,
  gatewayFailure = null,
}) {
  const observer = observerFromLocalHealth(localHealthPayload) ?? unknownObserver();
  const normalizedGateway = normalizeRuntimeCapability(gatewayPayload);
  let gateway = normalizedGateway?.gateway ?? unknownGateway();
  if (gatewayFailure !== null) {
    const code = SAFE_ERROR_CODES.has(gatewayFailure)
      ? gatewayFailure
      : "capability_invalid";
    const operational =
      code === "gateway_disabled"
        ? "disabled"
        : code === "gateway_starting"
          ? "starting"
          : code === "capability_invalid"
            ? "unknown"
            : "unavailable";
    gateway = {
      ...unknownGateway(),
      operational,
      lastError: {
        code,
        retryable: code === "gateway_timeout" || code === "gateway_unavailable",
      },
    };
  }
  return {
    apiVersion: API_VERSION,
    object: OBJECT_TYPE,
    observer,
    gateway,
  };
}

function observerFromLocalHealth(value) {
  try {
    if (!isRecord(value) || !isBoundedJson(value)) {
      return null;
    }
    const fields = readRequiredFields(value, [
      "schemaVersion",
      "dataRevision",
      "generatedAt",
      "health",
    ]);
    if (fields === null || !isRecord(fields.health)) return null;
    const health = readRequiredFields(fields.health, ["ok", "status"]);
    if (health === null || health.ok !== true || health.status !== "ok") {
      return null;
    }
    const dataRevision = nullableCount(fields.dataRevision);
    if (dataRevision === INVALID) return null;
    const generatedAt = nullableTimestamp(fields.generatedAt);
    const schemaVersion =
      typeof fields.schemaVersion === "string" &&
      fields.schemaVersion.length <= SCHEMA_VERSION_MAX_LENGTH &&
      SCHEMA_VERSION.test(fields.schemaVersion)
        ? fields.schemaVersion
        : null;
    return {
      operational: "ready",
      generatedAt,
      lastGoodAt: generatedAt,
      dataRevision,
      schemaVersion,
    };
  } catch {
    return null;
  }
}

function normalizeObserver(value) {
  try {
    if (!isRecord(value) || !isBoundedJson(value)) return unknownObserver();
    const operationalFact = readOwnData(value, "operational");
    const generatedAtFact = readOwnData(value, "generatedAt");
    const lastGoodAtFact = readOwnData(value, "lastGoodAt");
    const dataRevisionFact = readOwnData(value, "dataRevision");
    const schemaVersionFact = readOwnData(value, "schemaVersion");
    const dataRevision = nullableCount(
      dataRevisionFact.ok ? dataRevisionFact.value : undefined,
    );
    const schemaVersionValue = schemaVersionFact.ok
      ? schemaVersionFact.value
      : undefined;
    const schemaVersion =
      typeof schemaVersionValue === "string" &&
      schemaVersionValue.length <= SCHEMA_VERSION_MAX_LENGTH &&
      SCHEMA_VERSION.test(schemaVersionValue)
        ? schemaVersionValue
        : null;
    return {
      operational:
        operationalFact.ok && OPERATIONAL_VALUES.has(operationalFact.value)
        ? operationalFact.value
        : "unknown",
      generatedAt: nullableTimestamp(
        generatedAtFact.ok ? generatedAtFact.value : undefined,
      ),
      lastGoodAt: nullableTimestamp(
        lastGoodAtFact.ok ? lastGoodAtFact.value : undefined,
      ),
      dataRevision: dataRevision === INVALID ? null : dataRevision,
      schemaVersion,
    };
  } catch {
    return unknownObserver();
  }
}

function normalizeGateway(value) {
  try {
    return validatedGateway(value) ?? unknownGateway();
  } catch {
    return unknownGateway();
  }
}

function validatedGateway(value) {
  if (!isRecord(value) || !isBoundedJson(value)) return null;
  const required = readRequiredFields(value, ["mode", "operational", "features"]);
  if (required === null || typeof required.mode !== "string") return null;
  const mode = GATEWAY_MODES.has(required.mode) ? required.mode : "unknown";
  if (!OPERATIONAL_VALUES.has(required.operational)) return null;
  const rawFeatures = required.features;
  let observeListenerSupport = null;
  if (mode === "observe") {
    if (!isRecord(rawFeatures)) return null;
    const listenerFact = readOwnData(rawFeatures, "listener");
    if (!listenerFact.ok || !isRecord(listenerFact.value)) return null;
    const listenerFields = readRequiredFields(listenerFact.value, [
      "support",
      "enabled",
      "configured",
      "operational",
    ]);
    if (listenerFields === null) return null;
    const listener = listenerFields;
    if (
      !SUPPORT_VALUES.has(listener.support) ||
      listener.enabled !== false ||
      listener.configured !== false ||
      listener.operational !== "disabled"
    ) {
      return null;
    }
    observeListenerSupport = listener.support;
  }
  const features = normalizeFeatures(rawFeatures);
  if (features === null) return null;
  if (mode === "observe") {
    features.listener = {
      support: observeListenerSupport,
      enabled: false,
      configured: false,
      operational: "disabled",
    };
  }
  const configuredFact = readOptionalOwnData(value, "configuredProviderCount");
  const healthyFact = readOptionalOwnData(value, "healthyProviderCount");
  if (!configuredFact.ok || !healthyFact.ok) return null;
  const configuredProviderCount = nullableCount(
    configuredFact.present ? configuredFact.value : null,
  );
  const healthyProviderCount = nullableCount(
    healthyFact.present ? healthyFact.value : null,
  );
  if (
    configuredProviderCount === INVALID ||
    healthyProviderCount === INVALID ||
    (typeof configuredProviderCount === "number" &&
      typeof healthyProviderCount === "number" &&
      healthyProviderCount > configuredProviderCount)
  ) {
    return null;
  }
  const actionsFact = readOptionalOwnData(value, "actions");
  const errorFact = readOptionalOwnData(value, "lastError");
  if (!actionsFact.ok || !errorFact.ok) return null;
  const actions = normalizeActions(actionsFact.present ? actionsFact.value : []);
  if (actions === null) return null;
  return {
    mode,
    operational: required.operational,
    features,
    configuredProviderCount,
    healthyProviderCount,
    actions,
    lastError: normalizeLastError(errorFact.present ? errorFact.value : null),
  };
}

function normalizeFeatures(value) {
  if (!isRecord(value)) return null;
  const result = {};
  for (const featureId of FEATURE_IDS) {
    const featureFact = readOwnData(value, featureId);
    if (!featureFact.ok || !isRecord(featureFact.value)) return null;
    const feature = readRequiredFields(featureFact.value, [
      "support",
      "enabled",
      "configured",
      "operational",
    ]);
    if (feature === null) return null;
    if (
      !SUPPORT_VALUES.has(feature.support) ||
      !isKnownBoolean(feature.enabled) ||
      !isKnownBoolean(feature.configured) ||
      !OPERATIONAL_VALUES.has(feature.operational) ||
      (feature.operational === "ready" &&
        (feature.enabled === false ||
          feature.configured === false ||
          feature.support === "unsupported"))
    ) {
      return null;
    }
    result[featureId] =
      feature.support === "unsupported"
        ? {
            support: "unsupported",
            enabled: "unknown",
            configured: "unknown",
            operational: "unknown",
          }
        : {
            support: feature.support,
            enabled: feature.enabled,
            configured: feature.configured,
            operational: feature.operational,
          };
  }
  return result;
}

function normalizeActions(value) {
  if (!Array.isArray(value)) return null;
  const advertised = new Set(
    value.filter((item) => typeof item === "string" && ACTION_IDS.includes(item)),
  );
  return ACTION_IDS.filter((actionId) => advertised.has(actionId));
}

function normalizeLastError(value) {
  if (value == null) return null;
  if (!isRecord(value)) return invalidError();
  const required = readRequiredFields(value, ["code", "retryable"]);
  if (
    required === null ||
    !SAFE_ERROR_CODES.has(required.code) ||
    typeof required.retryable !== "boolean"
  ) {
    return invalidError();
  }
  const result = { code: required.code, retryable: required.retryable };
  const observedAtFact = readOptionalOwnData(value, "observedAt");
  if (!observedAtFact.ok) return invalidError();
  if (observedAtFact.present) {
    const observedAt = nullableTimestamp(observedAtFact.value);
    if (observedAt === null) return invalidError();
    result.observedAt = observedAt;
  }
  return result;
}

function nullableCount(value) {
  if (value == null) return null;
  if (!Number.isSafeInteger(value) || value < 0) return INVALID;
  return value;
}

function nullableTimestamp(value) {
  if (value == null || typeof value !== "string") return null;
  const match = TIMESTAMP.exec(value);
  if (match === null) return null;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return null;
  const [, year, month, day, hour, minute, second] = match;
  if (
    Number(year) < 1 ||
    instant.getUTCFullYear() !== Number(year) ||
    instant.getUTCMonth() + 1 !== Number(month) ||
    instant.getUTCDate() !== Number(day) ||
    instant.getUTCHours() !== Number(hour) ||
    instant.getUTCMinutes() !== Number(minute) ||
    instant.getUTCSeconds() !== Number(second)
  ) {
    return null;
  }
  return value;
}

function unknownObserver() {
  return {
    operational: "unknown",
    generatedAt: null,
    lastGoodAt: null,
    dataRevision: null,
    schemaVersion: null,
  };
}

function unknownGateway() {
  const features = {};
  for (const featureId of FEATURE_IDS) {
    features[featureId] = {
      support: "unknown",
      enabled: "unknown",
      configured: "unknown",
      operational: "unknown",
    };
  }
  return {
    mode: "unknown",
    operational: "unknown",
    features,
    configuredProviderCount: null,
    healthyProviderCount: null,
    actions: ["retry"],
    lastError: invalidError(),
  };
}

function invalidError() {
  return { code: "capability_invalid", retryable: false };
}

function isBoundedJson(value) {
  const counter = { value: 0 };
  if (!hasBoundedShape(value, 0, counter)) return false;
  try {
    const encoded = JSON.stringify(value);
    return (
      typeof encoded === "string" &&
      Buffer.byteLength(encoded, "utf8") <= SERIALIZED_UTF8_LIMIT
    );
  } catch {
    return false;
  }
}

function hasBoundedShape(value, depth, counter) {
  counter.value += 1;
  if (counter.value > MAX_INPUT_NODES || depth > MAX_INPUT_DEPTH) return false;
  if (value == null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value === "string") return isBoundedText(value);
  if (Array.isArray(value)) {
    return value.every((item) => hasBoundedShape(item, depth + 1, counter));
  }
  if (!isRecord(value)) return false;
  return Object.entries(value).every(
    ([key, item]) =>
      isBoundedText(key) && hasBoundedShape(item, depth + 1, counter),
  );
}

function isRecord(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

function isBoundedText(value) {
  let length = 0;
  for (const character of value) {
    length += 1;
    const code = character.codePointAt(0);
    if (
      length > MAX_TEXT_LENGTH ||
      code === undefined ||
      code < 32 ||
      code === 127 ||
      (code >= 0xd800 && code <= 0xdfff)
    ) {
      return false;
    }
  }
  return true;
}

function isKnownBoolean(value) {
  return typeof value === "boolean" || value === "unknown";
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function readOwnData(value, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      descriptor === undefined ||
      descriptor.enumerable !== true ||
      !Object.prototype.hasOwnProperty.call(descriptor, "value")
    ) {
      return { ok: false };
    }
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function readOptionalOwnData(value, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined) {
      return key in value
        ? { ok: false }
        : { ok: true, present: false, value: undefined };
    }
    if (
      descriptor.enumerable !== true ||
      !Object.prototype.hasOwnProperty.call(descriptor, "value")
    ) {
      return { ok: false };
    }
    return { ok: true, present: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function readRequiredFields(value, names) {
  const result = {};
  for (const name of names) {
    const fact = readOwnData(value, name);
    if (!fact.ok) return null;
    result[name] = fact.value;
  }
  return result;
}

module.exports = {
  API_VERSION,
  OBJECT_TYPE,
  composeRendererCapability,
  normalizeRuntimeCapability,
};
