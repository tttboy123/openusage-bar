export type AccountPoolStatus =
  | "ready"
  | "cooldown"
  | "quota_exhausted"
  | "disabled"
  | "backend_unavailable"
  | "unknown";

export type AccountQuotaState = "available" | "exhausted" | "unknown";
export type AccountCooldownState = "active" | "inactive" | "unknown";
export type AccountPoolStatusTone = "positive" | "warning" | "negative" | "neutral" | "unknown";
export type AccountPoolStrategy =
  | "fixed-first"
  | "round-robin"
  | "sticky"
  | "quota-aware"
  | "cost"
  | "latency"
  | "reliability";

export interface AccountQuota {
  state: AccountQuotaState;
  remaining: number | null;
  limit: number | null;
  resetAt: string | null;
}

export interface AccountCooldown {
  state: AccountCooldownState;
  until: string | null;
}

export interface AccountPoolMembership {
  poolId: string;
  priority: number;
  weight: number;
}

export interface AccountPoolAccount {
  alias: string | null;
  displayId: string;
  providerId: string;
  status: AccountPoolStatus;
  quota: AccountQuota;
  cooldown: AccountCooldown;
  pools: AccountPoolMembership[];
  priority: number;
  weight: number;
}

export interface AccountPoolMember {
  displayId: string;
  priority: number;
  weight: number;
}

export interface AccountPoolDefinition {
  poolId: string;
  revision: number;
  strategy: AccountPoolStrategy;
  members: AccountPoolMember[];
  crossProviderFallback: boolean;
  crossModelFallback: boolean;
  crossRegionFallback: boolean;
}

export interface AccountPools {
  accounts: AccountPoolAccount[];
  pools: AccountPoolDefinition[];
}

export type AccountPoolStatusKey =
  | "accountPoolStatusReady"
  | "accountPoolStatusCooldown"
  | "accountPoolStatusQuotaExhausted"
  | "accountPoolStatusDisabled"
  | "accountPoolStatusBackendUnavailable"
  | "accountPoolStatusUnknown";

export interface AccountPoolAccountViewModel extends AccountPoolAccount {
  statusKey: AccountPoolStatusKey;
  statusTone: AccountPoolStatusTone;
}

export interface AccountPoolsViewModel {
  state: "ready" | "empty" | "unknown";
  accounts: AccountPoolAccountViewModel[];
  pools: AccountPoolDefinition[];
  summary: {
    total: number;
    disabled: number;
    backendUnavailable: number;
    unknown: number;
  };
}

type JsonRecord = Record<PropertyKey, unknown>;

type OwnRead = { ok: true; value: unknown } | { ok: false; value?: never };

const ROOT_FIELDS = ["accounts", "pools"] as const;
const ACCOUNT_FIELDS = [
  "alias",
  "displayId",
  "providerId",
  "status",
  "quota",
  "cooldown",
  "pools",
  "priority",
  "weight",
] as const;
const QUOTA_FIELDS = ["state", "remaining", "limit", "resetAt"] as const;
const COOLDOWN_FIELDS = ["state", "until"] as const;
const MEMBERSHIP_FIELDS = ["poolId", "priority", "weight"] as const;
const POOL_FIELDS = [
  "poolId",
  "revision",
  "strategy",
  "members",
  "crossProviderFallback",
  "crossModelFallback",
  "crossRegionFallback",
] as const;
const POOL_MEMBER_FIELDS = ["displayId", "priority", "weight"] as const;

const STATUS_VALUES = new Set<AccountPoolStatus>([
  "ready",
  "cooldown",
  "quota_exhausted",
  "disabled",
  "backend_unavailable",
  "unknown",
]);
const QUOTA_STATE_VALUES = new Set<AccountQuotaState>([
  "available",
  "exhausted",
  "unknown",
]);
const COOLDOWN_STATE_VALUES = new Set<AccountCooldownState>([
  "active",
  "inactive",
  "unknown",
]);
const POOL_STRATEGY_VALUES = new Set<AccountPoolStrategy>([
  "fixed-first",
  "round-robin",
  "sticky",
  "quota-aware",
  "cost",
  "latency",
  "reliability",
]);

const MAX_TEXT_LENGTH = 128;
const MAX_TIMESTAMP_LENGTH = 64;
const MAX_ACCOUNTS = 256;
const MAX_MEMBERSHIPS = 64;
const MAX_POOLS = 64;
const MAX_POOL_MEMBERS = 64;
const MAX_PRIORITY = 1_000_000;
const MAX_WEIGHT = 1_000_000;
const MAX_POOL_PRIORITY = 10_000;
const MAX_POOL_WEIGHT = 100;
const ACCOUNT_DISPLAY_ID = /^acct_[0-9a-f]{12}$/u;
const PUBLIC_ID = /^[A-Za-z0-9._-]+$/u;

const UNKNOWN_MODEL = {
  state: "unknown",
  accounts: [],
  pools: [],
  summary: {
    total: 0,
    disabled: 0,
    backendUnavailable: 0,
    unknown: 0,
  },
} satisfies AccountPoolsViewModel;

export function normalizeAccountPools(value: unknown): AccountPools | null {
  try {
    if (!isRecord(value) || !exactOwnKeys(value, ROOT_FIELDS)) return null;
    const accountsRead = readOwn(value, "accounts");
    const poolsRead = readOwn(value, "pools");
    if (!accountsRead.ok || !poolsRead.ok) return null;
    const accountItems = exactDataArray(accountsRead.value, 0, MAX_ACCOUNTS);
    const poolItems = exactDataArray(poolsRead.value, 0, MAX_POOLS);
    if (accountItems === null || poolItems === null) return null;

    const accounts: AccountPoolAccount[] = [];
    const displayIds = new Set<string>();
    for (const item of accountItems) {
      const account = normalizeAccount(item);
      if (account === null || displayIds.has(account.displayId)) return null;
      displayIds.add(account.displayId);
      accounts.push(account);
    }
    const pools: AccountPoolDefinition[] = [];
    const poolIds = new Set<string>();
    for (const item of poolItems) {
      const pool = normalizePool(item, displayIds);
      if (pool === null || poolIds.has(pool.poolId)) return null;
      poolIds.add(pool.poolId);
      pools.push(pool);
    }
    if (
      accounts.some((account) =>
        account.pools.some((membership) => !poolIds.has(membership.poolId)),
      )
    ) {
      return null;
    }
    return { accounts, pools };
  } catch {
    return null;
  }
}

export function accountPoolsViewModel(value: unknown): AccountPoolsViewModel {
  const normalized = normalizeAccountPools(value);
  if (normalized === null) return cloneUnknownModel();

  const accounts = normalized.accounts.map((account) => ({
    ...account,
    statusKey: statusKey(account.status),
    statusTone: statusTone(account.status),
  }));

  return {
    state: accounts.length === 0 ? "empty" : "ready",
    accounts,
    pools: normalized.pools,
    summary: {
      total: accounts.length,
      disabled: accounts.filter((account) => account.status === "disabled").length,
      backendUnavailable: accounts.filter(
        (account) => account.status === "backend_unavailable",
      ).length,
      unknown: accounts.filter((account) => account.status === "unknown").length,
    },
  };
}

function normalizeAccount(value: unknown): AccountPoolAccount | null {
  if (!isRecord(value) || !exactOwnKeys(value, ACCOUNT_FIELDS)) return null;

  const alias = nullablePublicText(readValue(value, "alias"));
  const displayId = publicText(readValue(value, "displayId"));
  const providerId = publicIdentifier(readValue(value, "providerId"));
  const status = normalizeStatus(readValue(value, "status"));
  const quota = normalizeQuota(readValue(value, "quota"));
  const cooldown = normalizeCooldown(readValue(value, "cooldown"));
  const pools = normalizeMemberships(readValue(value, "pools"));
  const priority = boundedInteger(readValue(value, "priority"), MAX_PRIORITY);
  const weight = boundedInteger(readValue(value, "weight"), MAX_WEIGHT);

  if (
    alias === undefined ||
    displayId === null ||
    !ACCOUNT_DISPLAY_ID.test(displayId) ||
    providerId === null ||
    status === null ||
    quota === null ||
    cooldown === null ||
    pools === null ||
    priority === null ||
    weight === null
  ) {
    return null;
  }

  return {
    alias,
    displayId,
    providerId,
    status,
    quota,
    cooldown,
    pools,
    priority,
    weight,
  };
}

function normalizePool(
  value: unknown,
  accountDisplayIds: ReadonlySet<string>,
): AccountPoolDefinition | null {
  if (!isRecord(value) || !exactOwnKeys(value, POOL_FIELDS)) return null;
  const poolId = publicIdentifier(readValue(value, "poolId"));
  const revision = boundedInteger(readValue(value, "revision"), Number.MAX_SAFE_INTEGER);
  const strategyValue = readValue(value, "strategy");
  const strategy =
    typeof strategyValue === "string" &&
    POOL_STRATEGY_VALUES.has(strategyValue as AccountPoolStrategy)
      ? (strategyValue as AccountPoolStrategy)
      : null;
  const members = normalizePoolMembers(readValue(value, "members"), accountDisplayIds);
  const crossProviderFallback = readValue(value, "crossProviderFallback");
  const crossModelFallback = readValue(value, "crossModelFallback");
  const crossRegionFallback = readValue(value, "crossRegionFallback");
  if (
    poolId === null ||
    revision === null ||
    revision < 1 ||
    strategy === null ||
    members === null ||
    typeof crossProviderFallback !== "boolean" ||
    typeof crossModelFallback !== "boolean" ||
    typeof crossRegionFallback !== "boolean"
  ) {
    return null;
  }
  return {
    poolId,
    revision,
    strategy,
    members,
    crossProviderFallback,
    crossModelFallback,
    crossRegionFallback,
  };
}

function normalizePoolMembers(
  value: unknown,
  accountDisplayIds: ReadonlySet<string>,
): AccountPoolMember[] | null {
  const items = exactDataArray(value, 1, MAX_POOL_MEMBERS);
  if (items === null) return null;
  const members: AccountPoolMember[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    if (!isRecord(item) || !exactOwnKeys(item, POOL_MEMBER_FIELDS)) return null;
    const displayId = publicText(readValue(item, "displayId"));
    const priority = boundedInteger(readValue(item, "priority"), MAX_POOL_PRIORITY);
    const weight = boundedInteger(readValue(item, "weight"), MAX_POOL_WEIGHT);
    if (
      displayId === null ||
      !ACCOUNT_DISPLAY_ID.test(displayId) ||
      !accountDisplayIds.has(displayId) ||
      seen.has(displayId) ||
      priority === null ||
      weight === null ||
      weight < 1
    ) {
      return null;
    }
    seen.add(displayId);
    members.push({ displayId, priority, weight });
  }
  return members;
}

function normalizeQuota(value: unknown): AccountQuota | null {
  if (!isRecord(value) || !exactOwnKeys(value, QUOTA_FIELDS)) return null;
  const stateValue = readValue(value, "state");
  const state =
    typeof stateValue === "string" && QUOTA_STATE_VALUES.has(stateValue as AccountQuotaState)
      ? (stateValue as AccountQuotaState)
      : "unknown";
  const remaining = nullableFiniteNonNegative(readValue(value, "remaining"));
  const limit = nullableFiniteNonNegative(readValue(value, "limit"));
  const resetAt = nullableTimestamp(readValue(value, "resetAt"));
  if (remaining === undefined || limit === undefined || resetAt === undefined) {
    return null;
  }
  return {
    state,
    remaining,
    limit,
    resetAt,
  };
}

function normalizeCooldown(value: unknown): AccountCooldown | null {
  if (!isRecord(value) || !exactOwnKeys(value, COOLDOWN_FIELDS)) return null;
  const stateValue = readValue(value, "state");
  const state =
    typeof stateValue === "string" &&
    COOLDOWN_STATE_VALUES.has(stateValue as AccountCooldownState)
      ? (stateValue as AccountCooldownState)
      : "unknown";
  const until = nullableTimestamp(readValue(value, "until"));
  if (until === undefined) return null;
  return { state, until };
}

function normalizeMemberships(value: unknown): AccountPoolMembership[] | null {
  const items = exactDataArray(value, 0, MAX_MEMBERSHIPS);
  if (items === null) return null;
  const memberships: AccountPoolMembership[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    if (!isRecord(item) || !exactOwnKeys(item, MEMBERSHIP_FIELDS)) return null;
    const poolId = publicText(readValue(item, "poolId"));
    const priority = boundedInteger(readValue(item, "priority"), MAX_PRIORITY);
    const weight = boundedInteger(readValue(item, "weight"), MAX_WEIGHT);
    if (poolId === null || priority === null || weight === null || seen.has(poolId)) {
      return null;
    }
    seen.add(poolId);
    memberships.push({ poolId, priority, weight });
  }
  return memberships;
}

function normalizeStatus(value: unknown): AccountPoolStatus | null {
  if (typeof value !== "string" || value.length > MAX_TEXT_LENGTH) return null;
  return STATUS_VALUES.has(value as AccountPoolStatus)
    ? (value as AccountPoolStatus)
    : "unknown";
}

function statusKey(status: AccountPoolStatus): AccountPoolStatusKey {
  switch (status) {
    case "ready":
      return "accountPoolStatusReady";
    case "cooldown":
      return "accountPoolStatusCooldown";
    case "quota_exhausted":
      return "accountPoolStatusQuotaExhausted";
    case "disabled":
      return "accountPoolStatusDisabled";
    case "backend_unavailable":
      return "accountPoolStatusBackendUnavailable";
    case "unknown":
      return "accountPoolStatusUnknown";
  }
}

function statusTone(status: AccountPoolStatus): AccountPoolStatusTone {
  switch (status) {
    case "ready":
      return "positive";
    case "cooldown":
    case "backend_unavailable":
      return "warning";
    case "quota_exhausted":
      return "negative";
    case "disabled":
      return "neutral";
    case "unknown":
      return "unknown";
  }
}

function cloneUnknownModel(): AccountPoolsViewModel {
  return {
    state: UNKNOWN_MODEL.state,
    accounts: [],
    pools: [],
    summary: { ...UNKNOWN_MODEL.summary },
  };
}

function publicIdentifier(value: unknown): string | null {
  const text = publicText(value);
  return text !== null && PUBLIC_ID.test(text) ? text : null;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactOwnKeys(value: JsonRecord, expected: readonly string[]): boolean {
  try {
    const names = Object.getOwnPropertyNames(value);
    return (
      Object.getPrototypeOf(value) === Object.prototype &&
      Object.getOwnPropertySymbols(value).length === 0 &&
      names.length === expected.length &&
      expected.every((key) => names.includes(key))
    );
  } catch {
    return false;
  }
}

function exactDataArray(
  value: unknown,
  minimumLength: number,
  maximumLength: number,
): unknown[] | null {
  try {
    if (
      !Array.isArray(value) ||
      Object.getPrototypeOf(value) !== Array.prototype ||
      value.length < minimumLength ||
      value.length > maximumLength
    ) {
      return null;
    }
    const descriptors = Object.getOwnPropertyDescriptors(value);
    if (Reflect.ownKeys(descriptors).length !== value.length + 1) return null;
    const result: unknown[] = [];
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = descriptors[String(index)];
      if (!descriptor || !("value" in descriptor) || descriptor.enumerable !== true) {
        return null;
      }
      result.push(descriptor.value);
    }
    return result;
  } catch {
    return null;
  }
}

function readOwn(value: JsonRecord, key: string): OwnRead {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !("value" in descriptor)) return { ok: false };
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function readValue(value: JsonRecord, key: string): unknown {
  const read = readOwn(value, key);
  return read.ok ? read.value : undefined;
}

function publicText(value: unknown): string | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    Array.from(value).length > MAX_TEXT_LENGTH ||
    value !== value.trim() ||
    /\p{C}/u.test(value)
  ) {
    return null;
  }
  return value;
}

function nullablePublicText(value: unknown): string | null | undefined {
  if (value === null) return null;
  const text = publicText(value);
  return text === null ? undefined : text;
}

function boundedInteger(value: unknown, maximum: number): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0 &&
    value <= maximum
    ? value
    : null;
}

function nullableFiniteNonNegative(value: unknown): number | null | undefined {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

function nullableTimestamp(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_TIMESTAMP_LENGTH ||
    value !== value.trim() ||
    /\p{C}/u.test(value)
  ) {
    return undefined;
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?Z$/u.exec(
    value,
  );
  if (match === null) return undefined;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6] ?? "0");
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return undefined;
  }
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    0,
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return day >= 1 && day <= daysInMonth[month] ? value : undefined;
}
