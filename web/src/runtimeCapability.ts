export const RUNTIME_CAPABILITY_API_VERSION =
  "runtime-capability.openusage/v1" as const;
export const RUNTIME_CAPABILITY_OBJECT = "runtime.capability" as const;

const SERIALIZED_UTF8_LIMIT = 64 * 1024;
const MAX_TEXT_LENGTH = 256;
const MAX_SCHEMA_VERSION_LENGTH = 64;
const MAX_INPUT_DEPTH = 16;
const MAX_INPUT_NODES = 4096;
const MAX_SAFE_COUNT = Number.MAX_SAFE_INTEGER;

const SUPPORT_VALUES = ["supported", "unsupported", "unknown"] as const;
const OPERATIONAL_VALUES = [
  "disabled",
  "starting",
  "ready",
  "degraded",
  "unavailable",
  "unknown",
] as const;
const GATEWAY_MODES = ["observe", "advise", "gateway", "unknown"] as const;
const FEATURE_IDS = [
  "listener",
  "should_send",
  "responses",
  "cache",
  "fallback",
  "pii_redaction",
  "streaming",
] as const;
const ACTION_IDS = [
  "enable",
  "disable",
  "open_settings",
  "retry",
  "learn_more",
  "clear_cache",
] as const;
const SAFE_ERROR_CODES = [
  "gateway_disabled",
  "gateway_starting",
  "gateway_timeout",
  "gateway_unavailable",
  "provider_unavailable",
  "credential_backend_unavailable",
  "capability_invalid",
] as const;
const ROOT_FIELDS = new Set(["apiVersion", "object", "observer", "gateway"]);

const TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?Z$/;
const SCHEMA_VERSION = /^(?:openusage\/v\d+|\d+\.\d+)$/;
const INVALID = Symbol("invalid-runtime-capability-value");

export type Support = (typeof SUPPORT_VALUES)[number];
export type Operational = (typeof OPERATIONAL_VALUES)[number];
export type GatewayMode = (typeof GATEWAY_MODES)[number];
export type FeatureId = (typeof FEATURE_IDS)[number];
export type CapabilityActionId = (typeof ACTION_IDS)[number];
export type SafeErrorCode = (typeof SAFE_ERROR_CODES)[number];
export type KnownBoolean = boolean | "unknown";

export interface RuntimeFeatureCapability {
  support: Support;
  enabled: KnownBoolean;
  configured: KnownBoolean;
  operational: Operational;
}

export interface RuntimeObserverCapability {
  operational: Operational;
  generatedAt: string | null;
  lastGoodAt: string | null;
  dataRevision: number | null;
  schemaVersion: string | null;
}

export interface RuntimeCapabilityError {
  code: SafeErrorCode;
  retryable: boolean;
  observedAt?: string;
}

export interface RuntimeGatewayCapability {
  mode: GatewayMode;
  operational: Operational;
  features: Record<FeatureId, RuntimeFeatureCapability>;
  configuredProviderCount: number | null;
  healthyProviderCount: number | null;
  actions: CapabilityActionId[];
  lastError: RuntimeCapabilityError | null;
}

export interface RuntimeCapability {
  apiVersion: typeof RUNTIME_CAPABILITY_API_VERSION;
  object: typeof RUNTIME_CAPABILITY_OBJECT;
  observer: RuntimeObserverCapability;
  gateway: RuntimeGatewayCapability;
}

type JsonRecord = Record<string, unknown>;
type BoundedJson =
  | null
  | boolean
  | number
  | string
  | BoundedJson[]
  | { [key: string]: BoundedJson };

/**
 * Rebuild one untrusted wire value into the closed renderer-safe view model.
 * Rejected values are never logged, represented, or copied into the result.
 */
export function normalizeRuntimeCapability(value: unknown): RuntimeCapability | null {
  try {
    if (!isRecord(value)) return null;
    const apiVersion = readOwn(value, "apiVersion");
    const object = readOwn(value, "object");
    if (
      !apiVersion.ok ||
      apiVersion.value !== RUNTIME_CAPABILITY_API_VERSION ||
      !object.ok ||
      object.value !== RUNTIME_CAPABILITY_OBJECT ||
      !hasOwn(value, "observer") ||
      !hasOwn(value, "gateway")
    ) {
      return null;
    }

    const additive = Object.create(null) as JsonRecord;
    for (const key of Object.keys(value)) {
      if (!ROOT_FIELDS.has(key)) {
        const item = readOwn(value, key);
        if (!item.ok) return null;
        additive[key] = item.value;
      }
    }
    if (!isBoundedJson(additive)) return null;

    const observer = readOwn(value, "observer");
    const gateway = readOwn(value, "gateway");

    return {
      apiVersion: RUNTIME_CAPABILITY_API_VERSION,
      object: RUNTIME_CAPABILITY_OBJECT,
      observer: observer.ok ? normalizeObserver(observer.value) : unknownObserver(),
      gateway: gateway.ok ? normalizeGateway(gateway.value) : unknownGateway(),
    };
  } catch {
    return null;
  }
}

function normalizeObserver(value: unknown): RuntimeObserverCapability {
  if (!isRecord(value) || !isBoundedJson(value)) return unknownObserver();

  const rawOperational = readOwn(value, "operational");
  const rawGeneratedAt = readOwn(value, "generatedAt");
  const rawLastGoodAt = readOwn(value, "lastGoodAt");
  const rawDataRevision = readOwn(value, "dataRevision");
  const rawSchemaVersion = readOwn(value, "schemaVersion");
  const operational = rawOperational.ok && isOperational(rawOperational.value)
    ? rawOperational.value
    : "unknown";
  const dataRevision = nullableCount(
    rawDataRevision.ok ? rawDataRevision.value : undefined,
  );
  const schemaVersionValue = rawSchemaVersion.ok
    ? rawSchemaVersion.value
    : undefined;
  const schemaVersion =
    typeof schemaVersionValue === "string" &&
    schemaVersionValue.length <= MAX_SCHEMA_VERSION_LENGTH &&
    SCHEMA_VERSION.test(schemaVersionValue)
      ? schemaVersionValue
      : null;
  return {
    operational,
    generatedAt: nullableTimestamp(
      rawGeneratedAt.ok ? rawGeneratedAt.value : undefined,
    ),
    lastGoodAt: nullableTimestamp(
      rawLastGoodAt.ok ? rawLastGoodAt.value : undefined,
    ),
    dataRevision: dataRevision === INVALID ? null : dataRevision,
    schemaVersion,
  };
}

function normalizeGateway(value: unknown): RuntimeGatewayCapability {
  try {
    const normalized = validatedGateway(value);
    return normalized ?? unknownGateway();
  } catch {
    return unknownGateway();
  }
}

function validatedGateway(value: unknown): RuntimeGatewayCapability | null {
  if (!isRecord(value) || !isBoundedJson(value)) return null;
  const rawMode = readOwn(value, "mode");
  if (!rawMode.ok || typeof rawMode.value !== "string") return null;
  const mode: GatewayMode = isGatewayMode(rawMode.value)
    ? rawMode.value
    : "unknown";
  const rawOperational = readOwn(value, "operational");
  if (!rawOperational.ok || !isOperational(rawOperational.value)) return null;

  const rawFeaturesValue = readOwn(value, "features");
  if (!rawFeaturesValue.ok) return null;
  const rawFeatures = rawFeaturesValue.value;
  if (mode === "observe") {
    if (!isRecord(rawFeatures)) return null;
    const rawListenerValue = readOwn(rawFeatures, "listener");
    if (!rawListenerValue.ok || !isRecord(rawListenerValue.value)) return null;
    const rawListener = rawListenerValue.value;
    const rawEnabled = readOwn(rawListener, "enabled");
    const rawConfigured = readOwn(rawListener, "configured");
    const rawListenerOperational = readOwn(rawListener, "operational");
    if (
      !rawEnabled.ok ||
      rawEnabled.value !== false ||
      !rawConfigured.ok ||
      rawConfigured.value !== false ||
      !rawListenerOperational.ok ||
      rawListenerOperational.value !== "disabled"
    ) {
      return null;
    }
  }

  const features = normalizeFeatures(rawFeatures);
  if (features === null) return null;
  if (mode === "observe") {
    features.listener = {
      support: features.listener.support,
      enabled: false,
      configured: false,
      operational: "disabled",
    };
  }

  const rawConfiguredProviderCount = readOwn(value, "configuredProviderCount");
  const rawHealthyProviderCount = readOwn(value, "healthyProviderCount");
  const configuredProviderCount = nullableCount(
    rawConfiguredProviderCount.ok
      ? rawConfiguredProviderCount.value
      : undefined,
  );
  const healthyProviderCount = nullableCount(
    rawHealthyProviderCount.ok ? rawHealthyProviderCount.value : undefined,
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

  const rawActions = readOwn(value, "actions");
  const actions = normalizeActions(rawActions.ok ? rawActions.value : []);
  if (actions === null) return null;
  const rawLastError = readOwn(value, "lastError");
  return {
    mode,
    operational: rawOperational.value,
    features,
    configuredProviderCount,
    healthyProviderCount,
    actions,
    lastError: normalizeLastError(rawLastError.ok ? rawLastError.value : undefined),
  };
}

function normalizeFeatures(
  value: unknown,
): Record<FeatureId, RuntimeFeatureCapability> | null {
  if (!isRecord(value)) return null;
  const normalized = {} as Record<FeatureId, RuntimeFeatureCapability>;
  for (const featureId of FEATURE_IDS) {
    const rawValue = readOwn(value, featureId);
    if (!rawValue.ok || !isRecord(rawValue.value)) return null;
    const raw = rawValue.value;
    const support = readOwn(raw, "support");
    const enabled = readOwn(raw, "enabled");
    const configured = readOwn(raw, "configured");
    const operational = readOwn(raw, "operational");
    if (
      !support.ok ||
      !isSupport(support.value) ||
      !enabled.ok ||
      !isKnownBoolean(enabled.value) ||
      !configured.ok ||
      !isKnownBoolean(configured.value) ||
      !operational.ok ||
      !isOperational(operational.value) ||
      (operational.value === "ready" &&
        (enabled.value === false ||
          configured.value === false ||
          support.value === "unsupported"))
    ) {
      return null;
    }
    normalized[featureId] =
      support.value === "unsupported"
        ? {
            support: "unsupported",
            enabled: "unknown",
            configured: "unknown",
            operational: "unknown",
          }
        : {
            support: support.value,
            enabled: enabled.value,
            configured: configured.value,
            operational: operational.value,
          };
  }
  return normalized;
}

function normalizeActions(value: unknown): CapabilityActionId[] | null {
  if (!Array.isArray(value)) return null;
  const length = readOwn(value, "length");
  if (
    !length.ok ||
    typeof length.value !== "number" ||
    !Number.isSafeInteger(length.value)
  ) {
    return null;
  }
  const advertised = new Set<CapabilityActionId>();
  for (let index = 0; index < length.value; index += 1) {
    const item = readOwn(value, String(index));
    if (!item.ok) return null;
    if (isActionId(item.value)) advertised.add(item.value);
  }
  return ACTION_IDS.filter((actionId) => advertised.has(actionId));
}

function normalizeLastError(value: unknown): RuntimeCapabilityError | null {
  if (value == null) return null;
  if (!isRecord(value)) return invalidError();
  const code = readOwn(value, "code");
  const retryable = readOwn(value, "retryable");
  if (
    !code.ok ||
    !isSafeErrorCode(code.value) ||
    !retryable.ok ||
    typeof retryable.value !== "boolean"
  ) {
    return invalidError();
  }
  const result: RuntimeCapabilityError = {
    code: code.value,
    retryable: retryable.value,
  };
  if (hasOwn(value, "observedAt")) {
    const rawObservedAt = readOwn(value, "observedAt");
    if (!rawObservedAt.ok) return invalidError();
    const observedAt = nullableTimestamp(rawObservedAt.value);
    if (observedAt === null) return invalidError();
    result.observedAt = observedAt;
  }
  return result;
}

function nullableCount(value: unknown): number | null | typeof INVALID {
  if (value == null) return null;
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > MAX_SAFE_COUNT
  ) {
    return INVALID;
  }
  return value;
}

function nullableTimestamp(value: unknown): string | null {
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

function unknownObserver(): RuntimeObserverCapability {
  return {
    operational: "unknown",
    generatedAt: null,
    lastGoodAt: null,
    dataRevision: null,
    schemaVersion: null,
  };
}

function unknownGateway(): RuntimeGatewayCapability {
  const features = {} as Record<FeatureId, RuntimeFeatureCapability>;
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

function invalidError(): RuntimeCapabilityError {
  return { code: "capability_invalid", retryable: false };
}

function isBoundedJson(value: unknown): boolean {
  try {
    const counter = { value: 0 };
    const bounded = cloneBoundedJson(value, 0, counter);
    if (bounded === INVALID) return false;

    // Structured clone rejects Proxy objects before invoking their `get` trap.
    // Accessor properties have already been rejected by cloneBoundedJson.
    structuredClone(value);
    const encoded = JSON.stringify(bounded);
    return (
      typeof encoded === "string" &&
      new TextEncoder().encode(encoded).byteLength <= SERIALIZED_UTF8_LIMIT
    );
  } catch {
    return false;
  }
}

function cloneBoundedJson(
  value: unknown,
  depth: number,
  counter: { value: number },
): BoundedJson | typeof INVALID {
  counter.value += 1;
  if (counter.value > MAX_INPUT_NODES || depth > MAX_INPUT_DEPTH) return INVALID;
  if (value === null) return null;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : INVALID;
  if (typeof value === "string") return isBoundedText(value) ? value : INVALID;
  if (Array.isArray(value)) {
    const length = readOwn(value, "length");
    if (
      !length.ok ||
      typeof length.value !== "number" ||
      !Number.isSafeInteger(length.value) ||
      Object.keys(value).length !== length.value
    ) {
      return INVALID;
    }
    const result: BoundedJson[] = [];
    for (let index = 0; index < length.value; index += 1) {
      const item = readOwn(value, String(index));
      if (!item.ok) return INVALID;
      const boundedItem = cloneBoundedJson(item.value, depth + 1, counter);
      if (boundedItem === INVALID) return INVALID;
      result.push(boundedItem);
    }
    return result;
  }
  if (!isRecord(value)) return INVALID;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return INVALID;
  const result = Object.create(null) as { [key: string]: BoundedJson };
  for (const key of Object.keys(value)) {
    if (!isBoundedText(key)) return INVALID;
    const item = readOwn(value, key);
    if (!item.ok) return INVALID;
    const boundedItem = cloneBoundedJson(item.value, depth + 1, counter);
    if (boundedItem === INVALID) return INVALID;
    result[key] = boundedItem;
  }
  return result;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBoundedText(value: string): boolean {
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

function hasOwn(value: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function readOwn(
  value: object,
  key: PropertyKey,
): { ok: true; value: unknown } | { ok: false } {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined || !hasOwn(descriptor, "value")) {
      return { ok: false };
    }
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function includes<const T extends readonly string[]>(
  values: T,
  value: unknown,
): value is T[number] {
  return typeof value === "string" && values.includes(value as T[number]);
}

function isSupport(value: unknown): value is Support {
  return includes(SUPPORT_VALUES, value);
}

function isOperational(value: unknown): value is Operational {
  return includes(OPERATIONAL_VALUES, value);
}

function isGatewayMode(value: unknown): value is GatewayMode {
  return includes(GATEWAY_MODES, value);
}

function isActionId(value: unknown): value is CapabilityActionId {
  return includes(ACTION_IDS, value);
}

function isSafeErrorCode(value: unknown): value is SafeErrorCode {
  return includes(SAFE_ERROR_CODES, value);
}

function isKnownBoolean(value: unknown): value is KnownBoolean {
  return typeof value === "boolean" || value === "unknown";
}
