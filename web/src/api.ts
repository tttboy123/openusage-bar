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
}

export interface ActivityRow {
  day?: string;
  providerId?: string;
  sourceId?: string | null;
  modelId?: string;
  totalTokens?: number;
  inputTokens?: number;
  outputTokens?: number;
  costAmount?: string;
  costCurrency?: string;
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

export async function fetchActivity(from: string, to: string): Promise<ActivityRow[]> {
  const payload = await getJson<{ rows?: ActivityRow[] }>(
    `/v1/activity/daily?from=${from}&to=${to}`,
  );
  return payload.rows ?? [];
}

export async function fetchCosts(from: string, to: string): Promise<CostRow[]> {
  const payload = await getJson<{ rows?: CostRow[] }>(
    `/v1/costs/daily?from=${from}&to=${to}`,
  );
  return payload.rows ?? [];
}

export async function fetchSources(): Promise<SourceItem[]> {
  return getJson<SourceItem[]>("/v1/sources/status");
}

export async function fetchProviders(): Promise<ProviderItem[]> {
  return getJson<ProviderItem[]>("/v1/providers");
}

export async function fetchQuickConnect(): Promise<QuickConnectItem[]> {
  return getJson<QuickConnectItem[]>("/v1/quick-connect");
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}
