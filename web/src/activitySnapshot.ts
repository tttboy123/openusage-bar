import type {
  ActivityCoverageRow,
  ActivityResponse,
  ActivityRow,
} from "./api";

export interface ActivityRange {
  from: string;
  to: string;
}

export interface DerivedActivitySnapshot {
  schemaVersion: string;
  dataRevision: number;
  generatedAt: string;
  rows: ActivityRow[];
  coverage: ActivityCoverageRow[];
  periodRows: ActivityRow[];
  periodCoverage: ActivityCoverageRow[];
  dayTotals: Record<string, number>;
  periodDayTotals: Record<string, number>;
  periodTotal: number;
}

export interface LoadedActivitySnapshot {
  scopeKey: string;
  range: ActivityRange;
  response: ActivityResponse;
}

export interface ActivitySelection {
  providerId?: string;
  modelIds?: ReadonlySet<string>;
}

function isInRange(day: string | undefined, range: ActivityRange): boolean {
  return day !== undefined && day >= range.from && day <= range.to;
}

export function deriveActivitySnapshot(
  snapshot: ActivityResponse,
  periodRange: ActivityRange,
  selection: ActivitySelection = {},
): DerivedActivitySnapshot {
  const rows = snapshot.rows.filter(
    (row) =>
      (selection.providerId === undefined || row.providerId === selection.providerId) &&
      (selection.modelIds === undefined ||
        selection.modelIds.size === 0 ||
        (row.modelId !== undefined && selection.modelIds.has(row.modelId))),
  );
  const coverage =
    selection.providerId === undefined
      ? snapshot.coverage
      : snapshot.coverage.filter((row) => row.providerId === selection.providerId);
  const periodRows = rows.filter((row) => isInRange(row.day, periodRange));
  const periodCoverage = coverage.filter((row) => isInRange(row.day, periodRange));
  const dayTotals: Record<string, number> = {};

  for (const row of rows) {
    if (row.day === undefined) continue;
    dayTotals[row.day] = (dayTotals[row.day] ?? 0) + (row.totalTokens ?? 0);
  }

  const periodDayTotals: Record<string, number> = {};
  let periodTotal = 0;
  for (const [day, total] of Object.entries(dayTotals)) {
    if (!isInRange(day, periodRange)) continue;
    periodDayTotals[day] = total;
    periodTotal += total;
  }

  return {
    schemaVersion: snapshot.schemaVersion,
    dataRevision: snapshot.dataRevision,
    generatedAt: snapshot.generatedAt,
    rows,
    coverage,
    periodRows,
    periodCoverage,
    dayTotals,
    periodDayTotals,
    periodTotal,
  };
}

export function shouldReplaceActivitySnapshot(
  current: LoadedActivitySnapshot | null,
  candidate: LoadedActivitySnapshot,
): boolean {
  if (current === null) return true;
  const currentRevision = current.response.dataRevision;
  const candidateRevision = candidate.response.dataRevision;
  return candidateRevision >= currentRevision;
}

export function shouldPreserveActivitySnapshot(
  current: LoadedActivitySnapshot | null,
): boolean {
  return current !== null;
}
