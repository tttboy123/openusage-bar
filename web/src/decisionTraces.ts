const API_VERSION = "gateway-decision-trace.openusage/v1";
const MAX_TRACES = 128;
const MAX_EXCLUSIONS = 64;
const MAX_FACTS_WINDOW_SECONDS = 2_678_400;

const ROOT_FIELDS = ["apiVersion", "traces"] as const;
const TRACE_FIELDS = [
  "traceId",
  "occurredAt",
  "kind",
  "execution",
  "outcome",
  "reason",
  "pool",
  "selected",
  "exclusions",
  "fallback",
  "factsWindow",
] as const;
const POOL_FIELDS = ["poolId", "revision", "strategy"] as const;
const SELECTED_FIELDS = ["providerId", "accountDisplayId"] as const;
const EXCLUSION_FIELDS = ["accountDisplayId", "reason"] as const;
const FALLBACK_FIELDS = ["attempted", "attemptCount", "finalAction"] as const;
const FACTS_WINDOW_FIELDS = ["durationSeconds"] as const;

const TRACE_KINDS = new Set<DecisionTraceKind>([
  "route_advice",
  "gateway_execution",
  "pool_selection",
]);
const EXECUTIONS = new Set<DecisionTraceExecution>(["advice_only", "executed"]);
const OUTCOMES = new Set<DecisionTraceOutcome>([
  "yes",
  "no",
  "defer",
  "selected",
  "unavailable",
  "succeeded",
  "failed",
]);
const ROUTE_OUTCOMES = new Set<DecisionTraceOutcome>(["yes", "no", "defer"]);
const REASONS = new Set<DecisionTraceReason>([
  "approaching_limit",
  "burn_rate_too_high",
  "quota_healthy",
  "quota_low",
  "quota_unknown",
]);
const STRATEGIES = new Set<DecisionTraceStrategy>([
  "fixed-first",
  "round-robin",
  "sticky",
  "quota-aware",
  "cost",
  "latency",
  "reliability",
]);
const PROVIDERS = new Set<DecisionTraceProvider>([
  "anthropic",
  "deepseek",
  "ollama",
  "openai",
  "openrouter",
]);
const EXCLUSION_REASONS = new Set<DecisionTraceExclusionReason>([
  "cooldown",
  "credential_backend_unavailable",
  "cross_model_unconfirmed",
  "cross_provider_unconfirmed",
  "cross_region_unconfirmed",
  "disabled",
  "health_unknown",
  "metric_unknown",
  "quota_unknown",
  "unhealthy",
]);
const FALLBACK_ACTIONS = new Set<DecisionTraceFallbackAction>([
  "none",
  "retry",
  "fail",
  "degrade_to_cheap",
]);

const TRACE_ID = /^trace_[0-9a-f]{32}$/u;
const ACCOUNT_DISPLAY_ID = /^acct_[0-9a-f]{12}$/u;
const PUBLIC_ID = /^[A-Za-z0-9._-]+$/u;
const CANONICAL_UTC = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z$/u;

export type DecisionTraceKind =
  | "route_advice"
  | "gateway_execution"
  | "pool_selection";
export type DecisionTraceExecution = "advice_only" | "executed";
export type DecisionTraceOutcome =
  | "yes"
  | "no"
  | "defer"
  | "selected"
  | "unavailable"
  | "succeeded"
  | "failed";
export type DecisionTraceReason =
  | "approaching_limit"
  | "burn_rate_too_high"
  | "quota_healthy"
  | "quota_low"
  | "quota_unknown";
export type DecisionTraceStrategy =
  | "fixed-first"
  | "round-robin"
  | "sticky"
  | "quota-aware"
  | "cost"
  | "latency"
  | "reliability";
export type DecisionTraceProvider =
  | "anthropic"
  | "deepseek"
  | "ollama"
  | "openai"
  | "openrouter";
export type DecisionTraceExclusionReason =
  | "cooldown"
  | "credential_backend_unavailable"
  | "cross_model_unconfirmed"
  | "cross_provider_unconfirmed"
  | "cross_region_unconfirmed"
  | "disabled"
  | "health_unknown"
  | "metric_unknown"
  | "quota_unknown"
  | "unhealthy";
export type DecisionTraceFallbackAction =
  | "none"
  | "retry"
  | "fail"
  | "degrade_to_cheap";

export interface DecisionTracePool {
  poolId: string;
  revision: number;
  strategy: DecisionTraceStrategy;
}

export interface DecisionTraceSelection {
  providerId: DecisionTraceProvider | null;
  accountDisplayId: string | null;
}

export interface DecisionTraceExclusion {
  accountDisplayId: string;
  reason: DecisionTraceExclusionReason;
}

export interface DecisionTraceFallback {
  attempted: boolean;
  attemptCount: number;
  finalAction: DecisionTraceFallbackAction;
}

export interface DecisionTraceFactsWindow {
  durationSeconds: number;
}

export interface DecisionTrace {
  traceId: string;
  occurredAt: string;
  kind: DecisionTraceKind;
  execution: DecisionTraceExecution;
  outcome: DecisionTraceOutcome;
  reason: DecisionTraceReason | null;
  pool: DecisionTracePool | null;
  selected: DecisionTraceSelection | null;
  exclusions: DecisionTraceExclusion[];
  fallback: DecisionTraceFallback | null;
  factsWindow: DecisionTraceFactsWindow | null;
}

export interface DecisionTraces {
  apiVersion: typeof API_VERSION;
  traces: DecisionTrace[];
}

type JsonRecord = Record<PropertyKey, unknown>;
type OwnRead = { ok: true; value: unknown } | { ok: false; value?: never };

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
      expected.every(
        (key) => keys.includes(key) && enumerableKeys.includes(key),
      )
    );
  } catch {
    return false;
  }
}

function exactDataArray(value: unknown, maximum: number): unknown[] | null {
  if (!Array.isArray(value)) return null;
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
      keys.some(
        (key) =>
          typeof key !== "string" ||
          (key !== "length" && !/^(?:0|[1-9]\d*)$/u.test(key)) ||
          (key !== "length" && Number(key) >= length),
      )
    ) {
      return null;
    }
  } catch {
    return null;
  }
  const items: unknown[] = [];
  for (let index = 0; index < length; index += 1) {
    const itemRead = readOwn(value, String(index));
    if (!itemRead.ok) return null;
    items.push(itemRead.value);
  }
  return items;
}

function enumValue<T extends string>(
  value: unknown,
  values: ReadonlySet<T>,
): T | null {
  return typeof value === "string" && values.has(value as T)
    ? (value as T)
    : null;
}

function boundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
    ? value
    : null;
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

function normalizePool(value: unknown): DecisionTracePool | null {
  if (!isRecord(value) || !exactOwnKeys(value, POOL_FIELDS)) return null;
  const poolId = readValue(value, "poolId");
  const revision = boundedInteger(
    readValue(value, "revision"),
    1,
    Number.MAX_SAFE_INTEGER,
  );
  const strategy = enumValue(readValue(value, "strategy"), STRATEGIES);
  if (
    typeof poolId !== "string" ||
    poolId.length < 1 ||
    poolId.length > 128 ||
    !PUBLIC_ID.test(poolId) ||
    revision === null ||
    strategy === null
  ) {
    return null;
  }
  return { poolId, revision, strategy };
}

function normalizeSelection(value: unknown): DecisionTraceSelection | null {
  if (!isRecord(value) || !exactOwnKeys(value, SELECTED_FIELDS)) return null;
  const providerValue = readValue(value, "providerId");
  const accountValue = readValue(value, "accountDisplayId");
  const providerId =
    providerValue === null ? null : enumValue(providerValue, PROVIDERS);
  const accountDisplayId =
    accountValue === null
      ? null
      : typeof accountValue === "string" && ACCOUNT_DISPLAY_ID.test(accountValue)
        ? accountValue
        : undefined;
  if (
    (providerId === null && providerValue !== null) ||
    accountDisplayId === undefined
  ) {
    return null;
  }
  return { providerId, accountDisplayId };
}

function normalizeExclusions(value: unknown): DecisionTraceExclusion[] | null {
  const items = exactDataArray(value, MAX_EXCLUSIONS);
  if (items === null) return null;
  const exclusions: DecisionTraceExclusion[] = [];
  for (const item of items) {
    if (!isRecord(item) || !exactOwnKeys(item, EXCLUSION_FIELDS)) return null;
    const accountDisplayId = readValue(item, "accountDisplayId");
    const reason = enumValue(readValue(item, "reason"), EXCLUSION_REASONS);
    if (
      typeof accountDisplayId !== "string" ||
      !ACCOUNT_DISPLAY_ID.test(accountDisplayId) ||
      reason === null
    ) {
      return null;
    }
    exclusions.push({ accountDisplayId, reason });
  }
  return exclusions;
}

function normalizeFallback(value: unknown): DecisionTraceFallback | null {
  if (!isRecord(value) || !exactOwnKeys(value, FALLBACK_FIELDS)) return null;
  const attempted = readValue(value, "attempted");
  const attemptCount = boundedInteger(readValue(value, "attemptCount"), 0, 2);
  const finalAction = enumValue(readValue(value, "finalAction"), FALLBACK_ACTIONS);
  if (
    typeof attempted !== "boolean" ||
    attemptCount === null ||
    finalAction === null
  ) {
    return null;
  }
  if (!attempted) {
    return finalAction === "none" && attemptCount <= 1
      ? { attempted, attemptCount, finalAction }
      : null;
  }
  if (
    finalAction === "none" ||
    attemptCount < 1 ||
    ((finalAction === "retry" || finalAction === "degrade_to_cheap") &&
      attemptCount !== 2)
  ) {
    return null;
  }
  return { attempted, attemptCount, finalAction };
}

function normalizeFactsWindow(value: unknown): DecisionTraceFactsWindow | null {
  if (!isRecord(value) || !exactOwnKeys(value, FACTS_WINDOW_FIELDS)) return null;
  const durationSeconds = boundedInteger(
    readValue(value, "durationSeconds"),
    1,
    MAX_FACTS_WINDOW_SECONDS,
  );
  return durationSeconds === null ? null : { durationSeconds };
}

function normalizeTrace(value: unknown): DecisionTrace | null {
  if (!isRecord(value) || !exactOwnKeys(value, TRACE_FIELDS)) return null;
  const traceId = readValue(value, "traceId");
  const occurredAt = canonicalUtc(readValue(value, "occurredAt"));
  const kind = enumValue(readValue(value, "kind"), TRACE_KINDS);
  const execution = enumValue(readValue(value, "execution"), EXECUTIONS);
  const outcome = enumValue(readValue(value, "outcome"), OUTCOMES);
  const reasonValue = readValue(value, "reason");
  const reason = reasonValue === null ? null : enumValue(reasonValue, REASONS);
  const poolValue = readValue(value, "pool");
  const pool = poolValue === null ? null : normalizePool(poolValue);
  const selectedValue = readValue(value, "selected");
  const selected =
    selectedValue === null ? null : normalizeSelection(selectedValue);
  const exclusions = normalizeExclusions(readValue(value, "exclusions"));
  const fallbackValue = readValue(value, "fallback");
  const fallback =
    fallbackValue === null ? null : normalizeFallback(fallbackValue);
  const factsWindowValue = readValue(value, "factsWindow");
  const factsWindow =
    factsWindowValue === null
      ? null
      : normalizeFactsWindow(factsWindowValue);

  if (
    typeof traceId !== "string" ||
    !TRACE_ID.test(traceId) ||
    occurredAt === null ||
    kind === null ||
    execution === null ||
    outcome === null ||
    (reasonValue !== null && reason === null) ||
    (poolValue !== null && pool === null) ||
    (selectedValue !== null && selected === null) ||
    exclusions === null ||
    (fallbackValue !== null && fallback === null) ||
    (factsWindowValue !== null && factsWindow === null)
  ) {
    return null;
  }

  if (kind === "route_advice") {
    if (
      execution !== "advice_only" ||
      !ROUTE_OUTCOMES.has(outcome) ||
      reason === null ||
      pool !== null ||
      selected !== null ||
      exclusions.length !== 0 ||
      fallback !== null
    ) {
      return null;
    }
  } else if (kind === "gateway_execution") {
    if (
      execution !== "executed" ||
      (outcome !== "succeeded" && outcome !== "failed") ||
      reason !== null ||
      pool !== null ||
      selected === null ||
      selected.providerId === null ||
      selected.accountDisplayId !== null ||
      exclusions.length !== 0 ||
      fallback === null ||
      factsWindow !== null
    ) {
      return null;
    }
  } else if (
    execution !== "executed" ||
    (outcome !== "selected" && outcome !== "unavailable") ||
    reason !== null ||
    pool === null ||
    (outcome === "selected" &&
      (selected === null ||
        selected.providerId === null ||
        selected.accountDisplayId === null)) ||
    (outcome === "unavailable" && selected !== null) ||
    fallback !== null ||
    factsWindow !== null
  ) {
    return null;
  }

  return {
    traceId,
    occurredAt,
    kind,
    execution,
    outcome,
    reason,
    pool,
    selected,
    exclusions,
    fallback,
    factsWindow,
  };
}

export function normalizeDecisionTraces(value: unknown): DecisionTraces | null {
  try {
    if (!isRecord(value) || !exactOwnKeys(value, ROOT_FIELDS)) return null;
    if (readValue(value, "apiVersion") !== API_VERSION) return null;
    const items = exactDataArray(readValue(value, "traces"), MAX_TRACES);
    if (items === null) return null;

    const traces: DecisionTrace[] = [];
    const traceIds = new Set<string>();
    let previousOccurredAt: string | null = null;
    for (const item of items) {
      const trace = normalizeTrace(item);
      if (
        trace === null ||
        traceIds.has(trace.traceId) ||
        (previousOccurredAt !== null && trace.occurredAt > previousOccurredAt)
      ) {
        return null;
      }
      traceIds.add(trace.traceId);
      previousOccurredAt = trace.occurredAt;
      traces.push(trace);
    }
    return { apiVersion: API_VERSION, traces };
  } catch {
    return null;
  }
}
