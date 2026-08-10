export const PLUGIN_CONNECTIONS_API_VERSION = "plugin.openusage/v1" as const;
export const PLUGIN_CONNECTIONS_OBJECT = "plugin.connections" as const;

const ROOT_FIELDS = ["apiVersion", "object", "observedAt", "connections"] as const;
const CONNECTION_FIELDS = [
  "pluginId",
  "configuration",
  "connection",
  "capabilityState",
  "capabilities",
  "lastSeenAt",
  "lastSyncOutcome",
  "lastSyncAt",
] as const;
const PLUGIN_IDS = ["loom", "codex", "claude_code"] as const;
const CONFIGURATIONS = new Set<PluginConfiguration>([
  "configured",
  "not_configured",
  "unknown",
]);
const CONNECTIONS = new Set<PluginConnectionState>([
  "never_seen",
  "connected",
  "stale",
  "unknown",
]);
const CAPABILITY_STATES = new Set<PluginCapabilityState>([
  "not_negotiated",
  "negotiated",
  "incompatible",
  "unknown",
]);
const CAPABILITIES = new Set<PluginCapability>([
  "usage.query",
  "quotas.query",
  "health.query",
  "route.advice",
  "outcome.record",
  "decision.lookup",
]);
const NEGOTIATED_CAPABILITIES: readonly PluginCapability[] = [
  "decision.lookup",
  "health.query",
  "outcome.record",
  "quotas.query",
  "route.advice",
  "usage.query",
];
const SYNC_OUTCOMES = new Set<PluginSyncOutcome>([
  "never",
  "succeeded",
  "failed",
  "unknown",
]);
const CANONICAL_UTC =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z$/u;

export type PluginId = (typeof PLUGIN_IDS)[number];
export type PluginConfiguration = "configured" | "not_configured" | "unknown";
export type PluginConnectionState = "never_seen" | "connected" | "stale" | "unknown";
export type PluginCapabilityState =
  | "not_negotiated"
  | "negotiated"
  | "incompatible"
  | "unknown";
export type PluginCapability =
  | "usage.query"
  | "quotas.query"
  | "health.query"
  | "route.advice"
  | "outcome.record"
  | "decision.lookup";
export type PluginSyncOutcome = "never" | "succeeded" | "failed" | "unknown";

export interface PluginConnection {
  pluginId: PluginId;
  configuration: PluginConfiguration;
  connection: PluginConnectionState;
  capabilityState: PluginCapabilityState;
  capabilities: PluginCapability[];
  lastSeenAt: string | null;
  lastSyncOutcome: PluginSyncOutcome;
  lastSyncAt: string | null;
}

export interface PluginConnections {
  apiVersion: typeof PLUGIN_CONNECTIONS_API_VERSION;
  object: typeof PLUGIN_CONNECTIONS_OBJECT;
  observedAt: string;
  connections: PluginConnection[];
}

type JsonRecord = Record<PropertyKey, unknown>;
type OwnRead = { ok: true; value: unknown } | { ok: false; value?: never };

/** Rebuild one untrusted host value into the closed renderer-safe projection. */
export function normalizePluginConnections(value: unknown): PluginConnections | null {
  try {
    if (!isRecord(value) || !exactOwnKeys(value, ROOT_FIELDS)) return null;
    if (
      readValue(value, "apiVersion") !== PLUGIN_CONNECTIONS_API_VERSION ||
      readValue(value, "object") !== PLUGIN_CONNECTIONS_OBJECT
    ) {
      return null;
    }
    const observedAt = canonicalUtc(readValue(value, "observedAt"));
    const items = exactDataArray(readValue(value, "connections"), PLUGIN_IDS.length);
    if (observedAt === null || items === null || items.length !== PLUGIN_IDS.length) {
      return null;
    }

    const connections: PluginConnection[] = [];
    for (let index = 0; index < PLUGIN_IDS.length; index += 1) {
      const connection = normalizeConnection(items[index], PLUGIN_IDS[index]);
      if (connection === null) return null;
      connections.push(connection);
    }
    return {
      apiVersion: PLUGIN_CONNECTIONS_API_VERSION,
      object: PLUGIN_CONNECTIONS_OBJECT,
      observedAt,
      connections,
    };
  } catch {
    return null;
  }
}

function normalizeConnection(value: unknown, expectedPluginId: PluginId): PluginConnection | null {
  if (!isRecord(value) || !exactOwnKeys(value, CONNECTION_FIELDS)) return null;
  if (readValue(value, "pluginId") !== expectedPluginId) return null;

  const configuration = enumValue(readValue(value, "configuration"), CONFIGURATIONS);
  const connection = enumValue(readValue(value, "connection"), CONNECTIONS);
  const capabilityState = enumValue(
    readValue(value, "capabilityState"),
    CAPABILITY_STATES,
  );
  const lastSyncOutcome = enumValue(
    readValue(value, "lastSyncOutcome"),
    SYNC_OUTCOMES,
  );
  const lastSeenAt = nullableUtc(readValue(value, "lastSeenAt"));
  const lastSyncAt = nullableUtc(readValue(value, "lastSyncAt"));
  const capabilities = normalizeCapabilities(readValue(value, "capabilities"));
  if (
    configuration === null ||
    connection === null ||
    capabilityState === null ||
    lastSyncOutcome === null ||
    lastSeenAt === undefined ||
    lastSyncAt === undefined ||
    capabilities === null
  ) {
    return null;
  }
  if (
    (capabilityState === "negotiated"
      ? capabilities.length !== NEGOTIATED_CAPABILITIES.length ||
        capabilities.some(
          (capability, index) => capability !== NEGOTIATED_CAPABILITIES[index],
        )
      : capabilities.length !== 0) ||
    ((connection === "connected" || connection === "stale") &&
      (configuration !== "configured" || lastSeenAt === null)) ||
    (connection === "never_seen" && lastSeenAt !== null) ||
    (configuration === "not_configured" &&
      (connection !== "never_seen" ||
        capabilityState !== "not_negotiated" ||
        capabilities.length !== 0 ||
        lastSeenAt !== null ||
        lastSyncOutcome !== "never" ||
        lastSyncAt !== null)) ||
    ((lastSyncOutcome === "succeeded" || lastSyncOutcome === "failed") !==
      (lastSyncAt !== null))
  ) {
    return null;
  }

  return {
    pluginId: expectedPluginId,
    configuration,
    connection,
    capabilityState,
    capabilities,
    lastSeenAt,
    lastSyncOutcome,
    lastSyncAt,
  };
}

function normalizeCapabilities(value: unknown): PluginCapability[] | null {
  const items = exactDataArray(value, CAPABILITIES.size);
  if (items === null) return null;
  const normalized: PluginCapability[] = [];
  let previous: PluginCapability | null = null;
  for (const item of items) {
    const capability = enumValue(item, CAPABILITIES);
    if (capability === null || (previous !== null && capability <= previous)) {
      return null;
    }
    normalized.push(capability);
    previous = capability;
  }
  return normalized;
}

function isRecord(value: unknown): value is JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  try {
    return Object.getPrototypeOf(value) === Object.prototype;
  } catch {
    return false;
  }
}

function readOwn(value: object, key: PropertyKey): OwnRead {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !("value" in descriptor)) return { ok: false };
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function readValue(value: JsonRecord, key: PropertyKey): unknown {
  const result = readOwn(value, key);
  return result.ok ? result.value : undefined;
}

function exactOwnKeys(value: JsonRecord, expected: readonly string[]): boolean {
  try {
    const keys = Reflect.ownKeys(value);
    const enumerableKeys = Object.keys(value);
    return (
      keys.length === expected.length &&
      enumerableKeys.length === expected.length &&
      keys.every((key) => typeof key === "string") &&
      expected.every((key) => keys.includes(key) && enumerableKeys.includes(key))
    );
  } catch {
    return false;
  }
}

function exactDataArray(value: unknown, maximum: number): unknown[] | null {
  if (!Array.isArray(value)) return null;
  try {
    if (Object.getPrototypeOf(value) !== Array.prototype) return null;
  } catch {
    return null;
  }
  const lengthRead = readOwn(value, "length");
  if (
    !lengthRead.ok ||
    typeof lengthRead.value !== "number" ||
    !Number.isSafeInteger(lengthRead.value) ||
    lengthRead.value < 0 ||
    lengthRead.value > maximum
  ) {
    return null;
  }
  const length = lengthRead.value;
  try {
    const keys = Reflect.ownKeys(value);
    const enumerableKeys = Object.keys(value);
    if (
      keys.length !== length + 1 ||
      enumerableKeys.length !== length ||
      enumerableKeys.some((key, index) => key !== String(index)) ||
      keys.some((key) =>
        typeof key !== "string" ||
        (key !== "length" && (!/^(?:0|[1-9]\d*)$/u.test(key) || Number(key) >= length))
      )
    ) {
      return null;
    }
  } catch {
    return null;
  }
  const items: unknown[] = [];
  for (let index = 0; index < length; index += 1) {
    const item = readOwn(value, String(index));
    if (!item.ok) return null;
    items.push(item.value);
  }
  return items;
}

function enumValue<T extends string>(value: unknown, values: ReadonlySet<T>): T | null {
  return typeof value === "string" && values.has(value as T) ? (value as T) : null;
}

function nullableUtc(value: unknown): string | null | undefined {
  if (value === null) return null;
  return canonicalUtc(value) ?? undefined;
}

function canonicalUtc(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = CANONICAL_UTC.exec(value);
  if (match === null) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return null;
  }
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [0, 31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= days[month] ? value : null;
}
