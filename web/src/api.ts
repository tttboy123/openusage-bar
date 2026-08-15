import {
  normalizeRuntimeCapability,
  type RuntimeCapability,
} from "./runtimeCapability";
import {
  canonicalShouldSendRequest,
  normalizeShouldSendAdvice,
  type ShouldSendAdvice,
} from "./shouldSendAdvice";
import type { ObserverPlatformCapability } from "./observerPlatformCapability";
import type { DecisionTraces } from "./decisionTraces";
import type { PluginConnections } from "./pluginConnections";

const RUNTIME_CAPABILITY_DEADLINE_MS = 7_000;
const OBSERVER_PLATFORM_CAPABILITY_DEADLINE_MS = 7_000;
const SHOULD_SEND_DEADLINE_MS = 12_000;
const DECISION_TRACE_FRESHNESS_CAP_MS = 3_000;
const PLUGIN_CONNECTIONS_DEADLINE_MS = 5_000;

export interface SnapshotSummary {
  todayTokens?: number;
  modelCount?: number;
  coveredDayCount?: number;
}

export interface QuotaItem {
  currency?: string;
  totalAvailable?: string;
  providerCount?: number;
  provenance?: string[][];
}

export interface ProviderItem {
  providerId?: string;
  displayName?: string;
  sourceKind?: string;
  familyId?: string;
  category?: string;
}

export interface Snapshot {
  summary?: SnapshotSummary;
  quotaHub?: QuotaItem[];
  providers?: ProviderItem[];
}

export interface CapacityProvider {
  providerId?: string;
  quotaName?: string;
  unit?: string;
  used?: string;
  quotaLimit?: string;
  remaining?: string;
  remainingRatio?: number;
  resetsAt?: string;
  state?: string;
  appliesTo?: { kind?: string; modelIds?: string[] };
}

export interface ActivityCoverageRow {
  day?: string;
  providerId?: string;
  accountRef?: string | null;
  covered?: boolean;
  sourceId?: string | null;
}

export interface ActivityResponse {
  rows: ActivityRow[];
  coverage: ActivityCoverageRow[];
}

export interface ActivityRow {
  day?: string;
  providerId?: string;
  accountRef?: string | null;
  sourceId?: string | null;
  modelId?: string;
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheCreationTokens?: number;
  reasoningTokens?: number | null;
  totalTokens?: number;
  quality?: string;
  tokenCountingConvention?: string;
  costAmount?: string | null;
  costCurrency?: string | null;
}

export interface QuotaHistoryItem {
  snapshotId?: number;
  recordId?: string;
  observedAt?: string;
  providerId?: string;
  accountRef?: string | null;
  quotaName?: string;
  remainingRatio?: number | null;
  state?: string;
  stale?: boolean;
  sourceId?: string;
}

export interface ChangeItem {
  changeSeq?: number;
  recordType?: string;
  recordId?: string;
  revision?: number;
  changedAt?: string;
}

export interface CostRow {
  day?: string;
  providerId?: string;
  amount?: string;
  currency?: string;
}

export interface SourceItem {
  providerId?: string;
  sourceId?: string;
  state?: string;
  errorCode?: string | null;
  lastAttemptAt?: string | null;
  lastSuccessAt?: string | null;
  staleAt?: string | null;
}

export interface QuickConnectItem {
  familyId: string;
  consoleUrl: string;
  authModes: string[];
  apiKeyUrl?: string | null;
}

export async function fetchSnapshot(): Promise<Snapshot> {
  return getJson<Snapshot>("/v1/snapshot");
}

export async function fetchCapacity(): Promise<CapacityProvider[]> {
  const payload = await getJson<{ providers?: CapacityProvider[] }>("/v1/capacity");
  return payload.providers ?? [];
}

export async function fetchActivity(
  from: string,
  to: string,
  providerIds?: string[],
  modelIds?: string[],
  signal?: AbortSignal,
): Promise<ActivityResponse> {
  const params = new URLSearchParams({ from, to });
  if (providerIds?.length) params.set("providerIds", providerIds.join(","));
  if (modelIds?.length) params.set("modelIds", modelIds.join(","));
  const payload = await getJson<{
    rows?: ActivityRow[];
    coverage?: ActivityCoverageRow[];
  }>(`/v1/activity/daily?${params.toString()}`, { signal });
  return {
    rows: payload.rows ?? [],
    coverage: payload.coverage ?? [],
  };
}

export async function fetchQuotaHistory(
  providerId?: string,
  accountRef?: string,
  from?: string,
  to?: string,
  limit?: number,
): Promise<QuotaHistoryItem[]> {
  const params = new URLSearchParams();
  if (providerId) params.set("providerId", providerId);
  if (accountRef) params.set("accountRef", accountRef);
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (limit != null) params.set("limit", String(limit));
  const payload = await getJson<{ snapshots?: QuotaHistoryItem[] }>(
    `/v1/quotas/history?${params.toString()}`,
  );
  return payload.snapshots ?? [];
}

export async function fetchChanges(after = 0, limit = 100): Promise<ChangeItem[]> {
  const payload = await getJson<{
    records?: ChangeItem[];
    nextCursor?: number;
    hasMore?: boolean;
  }>(`/v1/changes?after=${after}&limit=${limit}`);
  return payload.records ?? [];
}

export async function fetchCosts(from: string, to: string): Promise<CostRow[]> {
  const payload = await getJson<{ rows?: CostRow[] }>(
    `/v1/costs/daily?from=${from}&to=${to}`,
  );
  return payload.rows ?? [];
}

export async function fetchSources(): Promise<SourceItem[]> {
  const payload = await getJson<{ sources?: SourceItem[] }>("/v1/sources/status");
  return payload.sources ?? [];
}

export async function fetchProviders(): Promise<ProviderItem[]> {
  const payload = await getJson<{ providers?: ProviderItem[] }>("/v1/providers");
  return payload.providers ?? [];
}

export async function fetchQuickConnect(): Promise<QuickConnectItem[]> {
  const payload = await getJson<{ providers?: QuickConnectItem[] }>(
    "/v1/quick-connect",
  );
  return payload.providers ?? [];
}

 export interface HealthResult {
   schemaVersion?: string;
   dataRevision?: number;
   generatedAt?: string;
   sources?: SourceItem[];
   health?: { ok?: boolean; status?: string };
 }

 export interface SchemaResult {
   schemaVersion?: string;
   dataRevision?: number;
   generatedAt?: string;
   routes?: string[];
   errorShape?: { error?: { code?: string; message?: string } };
 }

 export async function fetchHealth(): Promise<HealthResult> {
   return getJson<HealthResult>("/v1/health");
 }

 export async function fetchSchema(): Promise<SchemaResult> {
   return getJson<SchemaResult>("/v1/schema");
 }

export async function fetchRuntimeCapability(): Promise<RuntimeCapability | null> {
  const payload = await getHostJsonWithin<unknown>(
    "/gateway/v1/health",
    RUNTIME_CAPABILITY_DEADLINE_MS,
    {
      method: "GET",
      credentials: "omit",
    },
  );
  return normalizeRuntimeCapability(payload);
}

export async function fetchObserverPlatformCapability(): Promise<
  ObserverPlatformCapability | null
> {
  const { normalizeObserverPlatformCapability } = await import(
    "./observerPlatformCapability"
  );
  const payload = await getHostJsonWithin<unknown>(
    "/v1/capabilities",
    OBSERVER_PLATFORM_CAPABILITY_DEADLINE_MS,
    {
      method: "GET",
      credentials: "omit",
    },
  );
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return null;
  }
  const projection = Object.getOwnPropertyDescriptor(payload, "observerPlatform");
  if (!projection || !("value" in projection)) return null;
  return normalizeObserverPlatformCapability(projection.value);
}

export async function fetchShouldSendAdvice(
  request: unknown,
): Promise<ShouldSendAdvice> {
  const body = canonicalShouldSendRequest(request);
  const payload = await getHostJsonWithin<unknown>(
    "/gateway/v1/should-send",
    SHOULD_SEND_DEADLINE_MS,
    {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body,
    },
  );
  return normalizeShouldSendAdvice(payload);
}

export async function fetchDecisionTraces(): Promise<DecisionTraces | null> {
  const { normalizeDecisionTraces } = await import("./decisionTraces");
  const controller = new AbortController();
  const deadline = globalThis.setTimeout(
    () => controller.abort(),
    DECISION_TRACE_FRESHNESS_CAP_MS,
  );
  try {
    const payload = await getJson<unknown>("/gateway/v1/decision-traces", {
      method: "GET",
      credentials: "omit",
      signal: controller.signal,
    });
    return normalizeDecisionTraces(payload);
  } finally {
    globalThis.clearTimeout(deadline);
  }
}

export async function fetchPluginConnections(): Promise<PluginConnections | null> {
  const { normalizePluginConnections } = await import("./pluginConnections");
  const payload = await getHostJsonWithin<unknown>(
    "/host/v1/plugin-connections",
    PLUGIN_CONNECTIONS_DEADLINE_MS,
    {
      method: "GET",
      credentials: "omit",
    },
  );
  return normalizePluginConnections(payload);
}

async function getHostJsonWithin<T>(
  path: string,
  deadlineMs: number,
  init: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  const deadline = globalThis.setTimeout(() => controller.abort(), deadlineMs);
  try {
    return await getJson<T>(path, { ...init, signal: controller.signal });
  } finally {
    globalThis.clearTimeout(deadline);
  }
}

export async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export interface RefreshStatus {
  status?: string;
  lastStartedAt?: string | null;
  lastFinishedAt?: string | null;
  succeeded?: boolean | null;
}

/**
 * Ask the local dashboard observer to run one bounded refresh with a full
 * history backfill. The renderer never touches credentials or the ledger
 * directly; this is a read-only refresh hint handled by the host.
 */
export async function triggerRefresh(): Promise<RefreshStatus> {
  const response = await fetch("/v1/refresh", { method: "POST", cache: "no-store" });
  if (!response.ok) {
    throw new Error(`/v1/refresh failed: ${response.status}`);
  }
  return (await response.json()) as RefreshStatus;
}

export async function fetchRefreshStatus(): Promise<RefreshStatus> {
  return getJson<RefreshStatus>("/v1/refresh/status");
}
