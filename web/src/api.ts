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

export async function fetchSnapshot(): Promise<Snapshot> {
  const response = await fetch("/v1/snapshot", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`snapshot failed: ${response.status}`);
  }
  return (await response.json()) as Snapshot;
}
