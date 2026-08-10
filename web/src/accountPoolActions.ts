export const HOST_ACTION_API_VERSION = "host-action.openusage/v1" as const;

export const ACCOUNT_POOL_HOST_ACTIONS = [
  "accountPool.create",
  "accountPool.edit",
  "accountPool.remove",
] as const;

export type AccountPoolHostAction = (typeof ACCOUNT_POOL_HOST_ACTIONS)[number];

export interface TrustedHostCapability {
  apiVersion: typeof HOST_ACTION_API_VERSION;
  actions: AccountPoolHostAction[];
}

export interface AccountPoolHostAdapter {
  readCapability(signal?: AbortSignal): Promise<unknown>;
  mutate(request: PoolHostActionRequest, signal?: AbortSignal): Promise<unknown>;
}

export type AccountPoolStrategy =
  | "fixed-first"
  | "round-robin"
  | "sticky"
  | "quota-aware"
  | "cost"
  | "latency"
  | "reliability";

export interface EditablePoolMember {
  displayId: string;
  priority: number;
  weight: number;
}

export interface EditablePool {
  poolId: string;
  strategy: AccountPoolStrategy;
  members: EditablePoolMember[];
  crossProviderFallback: boolean;
  crossModelFallback: boolean;
  crossRegionFallback: boolean;
}

export type PoolHostActionRequest =
  | {
      apiVersion: typeof HOST_ACTION_API_VERSION;
      action: "accountPool.create" | "accountPool.edit";
      expectedRevision: number | null;
      pool: EditablePool;
    }
  | {
      apiVersion: typeof HOST_ACTION_API_VERSION;
      action: "accountPool.remove";
      expectedRevision: number;
      pool: { poolId: string };
    };

export type PoolHostActionFailureCode =
  | "invalid_request"
  | "already_exists"
  | "not_found"
  | "revision_conflict"
  | "pool_references_unknown_account"
  | "lock_unavailable"
  | "config_write_failed"
  | "service_unavailable";

export type PoolHostActionResult =
  | {
      apiVersion: typeof HOST_ACTION_API_VERSION;
      action: AccountPoolHostAction;
      ok: true;
      code: "ok";
      pool: { poolId: string; revision: number };
    }
  | {
      apiVersion: typeof HOST_ACTION_API_VERSION;
      action: AccountPoolHostAction;
      ok: false;
      code: PoolHostActionFailureCode;
    };

type JsonRecord = Record<PropertyKey, unknown>;

const POOL_STRATEGIES = new Set<AccountPoolStrategy>([
  "fixed-first",
  "round-robin",
  "sticky",
  "quota-aware",
  "cost",
  "latency",
  "reliability",
]);
const DISPLAY_ID = /^acct_[0-9a-f]{12}$/u;
const PUBLIC_ID = /^[A-Za-z0-9._-]+$/u;
const FAILURE_CODES = new Set<PoolHostActionFailureCode>([
  "invalid_request",
  "already_exists",
  "not_found",
  "revision_conflict",
  "pool_references_unknown_account",
  "lock_unavailable",
  "config_write_failed",
  "service_unavailable",
]);
const HOST_CAPABILITY_PATH = "/host/v1/capabilities";
const HOST_ACTION_PATH = "/host/v1/actions";

export function createBrowserAccountPoolHostAdapter(
  fetcher: typeof fetch = fetch,
): AccountPoolHostAdapter {
  return {
    async readCapability(signal) {
      const response = await fetcher(HOST_CAPABILITY_PATH, {
        method: "GET",
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
        signal,
      });
      return response.ok ? response.json() : null;
    },
    async mutate(request, signal) {
      const response = await fetcher(HOST_ACTION_PATH, {
        method: "POST",
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal,
      });
      return response.ok ? response.json() : null;
    },
  };
}

export function buildPoolHostAction(
  action: unknown,
  poolValue: unknown,
  expectedRevision: unknown,
): PoolHostActionRequest | null {
  try {
    if (action === "accountPool.remove") {
      if (!validRevision(expectedRevision)) return null;
      const pool = exactPoolId(poolValue);
      return pool === null
        ? null
        : {
            apiVersion: HOST_ACTION_API_VERSION,
            action,
            expectedRevision,
            pool,
          };
    }
    if (action !== "accountPool.create" && action !== "accountPool.edit") {
      return null;
    }
    if (
      (action === "accountPool.create" && expectedRevision !== null) ||
      (action === "accountPool.edit" && !validRevision(expectedRevision))
    ) {
      return null;
    }
    const pool = exactEditablePool(poolValue);
    if (pool === null) return null;
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action,
      expectedRevision:
        action === "accountPool.create" ? null : (expectedRevision as number),
      pool,
    };
  } catch {
    return null;
  }
}

export function normalizePoolHostActionResult(
  value: unknown,
): PoolHostActionResult | null {
  try {
    if (!isRecord(value)) return null;
    const ok = readDataValue(value, "ok");
    const expectedKeys = ok === true
      ? ["apiVersion", "action", "ok", "code", "pool"]
      : ["apiVersion", "action", "ok", "code"];
    if (!exactOwnKeys(value, expectedKeys)) return null;
    const apiVersion = readDataValue(value, "apiVersion");
    const action = readDataValue(value, "action");
    const code = readDataValue(value, "code");
    if (
      apiVersion !== HOST_ACTION_API_VERSION ||
      typeof action !== "string" ||
      !ACCOUNT_POOL_HOST_ACTIONS.includes(action as AccountPoolHostAction)
    ) {
      return null;
    }
    if (ok === true) {
      if (code !== "ok") return null;
      const pool = exactPoolResult(readDataValue(value, "pool"));
      return pool === null
        ? null
        : {
            apiVersion: HOST_ACTION_API_VERSION,
            action: action as AccountPoolHostAction,
            ok: true,
            code: "ok",
            pool,
          };
    }
    if (
      ok !== false ||
      typeof code !== "string" ||
      !FAILURE_CODES.has(code as PoolHostActionFailureCode)
    ) {
      return null;
    }
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action: action as AccountPoolHostAction,
      ok: false,
      code: code as PoolHostActionFailureCode,
    };
  } catch {
    return null;
  }
}

export function normalizeTrustedHostCapability(
  value: unknown,
): TrustedHostCapability | null {
  try {
    if (!isRecord(value) || !exactOwnKeys(value, ["apiVersion", "actions"])) {
      return null;
    }
    const apiVersion = readDataValue(value, "apiVersion");
    const actionsValue = exactDataArray(
      readDataValue(value, "actions"),
      ACCOUNT_POOL_HOST_ACTIONS.length,
      ACCOUNT_POOL_HOST_ACTIONS.length,
    );
    if (
      apiVersion !== HOST_ACTION_API_VERSION ||
      actionsValue === null
    ) {
      return null;
    }
    const actions = new Set(actionsValue);
    if (
      actions.size !== ACCOUNT_POOL_HOST_ACTIONS.length ||
      ACCOUNT_POOL_HOST_ACTIONS.some((action) => !actions.has(action))
    ) {
      return null;
    }
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      actions: [...ACCOUNT_POOL_HOST_ACTIONS],
    };
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function exactPoolId(value: unknown): { poolId: string } | null {
  if (!isRecord(value) || !exactOwnKeys(value, ["poolId"])) return null;
  const poolId = readDataValue(value, "poolId");
  return validPublicId(poolId) ? { poolId } : null;
}

function exactPoolResult(
  value: unknown,
): { poolId: string; revision: number } | null {
  if (!isRecord(value) || !exactOwnKeys(value, ["poolId", "revision"])) {
    return null;
  }
  const poolId = readDataValue(value, "poolId");
  const revision = readDataValue(value, "revision");
  return validPublicId(poolId) && validRevision(revision)
    ? { poolId, revision }
    : null;
}

function exactEditablePool(value: unknown): EditablePool | null {
  if (
    !isRecord(value) ||
    !exactOwnKeys(value, [
      "poolId",
      "strategy",
      "members",
      "crossProviderFallback",
      "crossModelFallback",
      "crossRegionFallback",
    ])
  ) {
    return null;
  }
  const poolId = readDataValue(value, "poolId");
  const strategy = readDataValue(value, "strategy");
  const members = exactPoolMembers(readDataValue(value, "members"));
  const crossProviderFallback = readDataValue(value, "crossProviderFallback");
  const crossModelFallback = readDataValue(value, "crossModelFallback");
  const crossRegionFallback = readDataValue(value, "crossRegionFallback");
  if (
    !validPublicId(poolId) ||
    typeof strategy !== "string" ||
    !POOL_STRATEGIES.has(strategy as AccountPoolStrategy) ||
    members === null ||
    typeof crossProviderFallback !== "boolean" ||
    typeof crossModelFallback !== "boolean" ||
    typeof crossRegionFallback !== "boolean"
  ) {
    return null;
  }
  return {
    poolId,
    strategy: strategy as AccountPoolStrategy,
    members,
    crossProviderFallback,
    crossModelFallback,
    crossRegionFallback,
  };
}

function exactPoolMembers(value: unknown): EditablePoolMember[] | null {
  const items = exactDataArray(value, 1, 64);
  if (items === null) return null;
  const result: EditablePoolMember[] = [];
  const seen = new Set<string>();
  for (const memberValue of items) {
    if (
      !isRecord(memberValue) ||
      !exactOwnKeys(memberValue, ["displayId", "priority", "weight"])
    ) {
      return null;
    }
    const displayId = readDataValue(memberValue, "displayId");
    const priority = readDataValue(memberValue, "priority");
    const weight = readDataValue(memberValue, "weight");
    if (
      typeof displayId !== "string" ||
      !DISPLAY_ID.test(displayId) ||
      seen.has(displayId) ||
      !Number.isSafeInteger(priority) ||
      (priority as number) < 0 ||
      (priority as number) > 10_000 ||
      !Number.isSafeInteger(weight) ||
      (weight as number) < 1 ||
      (weight as number) > 100
    ) {
      return null;
    }
    seen.add(displayId);
    result.push({
      displayId,
      priority: priority as number,
      weight: weight as number,
    });
  }
  return result;
}

function exactDataArray(
  value: unknown,
  minimumLength: number,
  maximumLength: number,
): unknown[] | null {
  if (
    !Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Array.prototype ||
    value.length < minimumLength ||
    value.length > maximumLength
  ) {
    return null;
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const ownKeys = Reflect.ownKeys(descriptors);
  if (ownKeys.length !== value.length + 1) return null;
  const result: unknown[] = [];
  for (let index = 0; index < value.length; index += 1) {
    const descriptor = descriptors[String(index)];
    if (!descriptor || !("value" in descriptor) || descriptor.enumerable !== true) {
      return null;
    }
    result.push(descriptor.value);
  }
  return result;
}

function validRevision(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}

function validPublicId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    PUBLIC_ID.test(value)
  );
}

function exactOwnKeys(value: JsonRecord, expected: readonly string[]): boolean {
  const names = Object.getOwnPropertyNames(value);
  return (
    Object.getOwnPropertySymbols(value).length === 0 &&
    names.length === expected.length &&
    expected.every((key) => names.includes(key))
  );
}

function readDataValue(value: JsonRecord, key: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
}
