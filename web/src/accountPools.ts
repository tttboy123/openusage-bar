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
  status: AccountPoolStatus;
  quota: AccountQuota;
  cooldown: AccountCooldown;
  pools: AccountPoolMembership[];
  priority: number;
  weight: number;
}

export interface AccountPools {
  accounts: AccountPoolAccount[];
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
  summary: {
    total: number;
    disabled: number;
    backendUnavailable: number;
    unknown: number;
  };
}

type JsonRecord = Record<PropertyKey, unknown>;

type OwnRead = { ok: true; value: unknown } | { ok: false; value?: never };

const ROOT_FIELDS = ["accounts"] as const;
const ACCOUNT_FIELDS = [
  "alias",
  "displayId",
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

const MAX_TEXT_LENGTH = 128;
const MAX_TIMESTAMP_LENGTH = 64;
const MAX_ACCOUNTS = 256;
const MAX_MEMBERSHIPS = 64;
const MAX_PRIORITY = 1_000_000;
const MAX_WEIGHT = 1_000_000;
const ACCOUNT_DISPLAY_ID = /^acct_[0-9a-f]{12}$/u;

const UNKNOWN_MODEL = {
  state: "unknown",
  accounts: [],
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
    if (!accountsRead.ok || !Array.isArray(accountsRead.value)) return null;
    if (accountsRead.value.length > MAX_ACCOUNTS) return null;

    const accounts: AccountPoolAccount[] = [];
    for (const item of accountsRead.value) {
      const account = normalizeAccount(item);
      if (account === null) return null;
      accounts.push(account);
    }
    return { accounts };
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
    status,
    quota,
    cooldown,
    pools,
    priority,
    weight,
  };
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
  if (!Array.isArray(value) || value.length > MAX_MEMBERSHIPS) return null;
  const memberships: AccountPoolMembership[] = [];
  const seen = new Set<string>();
  for (const item of value) {
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
    summary: { ...UNKNOWN_MODEL.summary },
  };
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactOwnKeys(value: JsonRecord, expected: readonly string[]): boolean {
  try {
    const names = Object.getOwnPropertyNames(value);
    return (
      Object.getOwnPropertySymbols(value).length === 0 &&
      names.length === expected.length &&
      expected.every((key) => names.includes(key))
    );
  } catch {
    return false;
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
