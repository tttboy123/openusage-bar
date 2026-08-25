import { useEffect, useMemo, useRef, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { Funnel } from "@phosphor-icons/react";
import {
  fetchActivity,
  fetchProviders,
  fetchSnapshot,
  type Snapshot,
  type ActivityRow,
  type ActivityCoverageRow,
} from "../api";
import {
  deriveActivitySnapshot,
  shouldPreserveActivitySnapshot,
  shouldReplaceActivitySnapshot,
  type LoadedActivitySnapshot,
} from "../activitySnapshot";
import { useAnimatedNumber } from "../hooks/useAnimatedNumber";
import Skeleton from "../components/Skeleton";
import PeriodSelector, {
  eachDay,
  endOfWeek,
  localDayKey,
  parseDayKey,
  periodDays,
  rangeFor,
  rangeLength,
  startOfWeek,
  type Period,
} from "../components/PeriodSelector";
import { type Messages, tpl } from "../i18n";
import { formatTokenCompact } from "../tokenFormat";

type BadgeState = "partial" | "missing" | "neutral";

function KpiV2({
  label,
  value,
  meta,
  badge,
}: {
  label: string;
  value: string;
  meta?: string;
  badge?: { state: BadgeState; text: string };
}) {
  return (
    <div className="metric">
      <p className="metric-value">{value}</p>
      <p className="metric-label">{label}</p>
      {badge ? (
        <span className={`metric-badge metric-badge-${badge.state}`}>
          {badge.text}
        </span>
      ) : null}
      {meta ? <p className="metric-meta">{meta}</p> : null}
    </div>
  );
}

const PERIOD_LABEL: Record<Period, keyof Messages> = {
  day: "periodDay",
  week: "periodWeek",
  month: "periodMonth",
  year: "periodYear",
};

const HEATMAP_CELL_SIZE = 13;
const HEATMAP_GAP = 4;

const MODEL_COLORS = [
  "#4f6f8f",
  "#6b8f71",
  "#b38b4f",
  "#8a6fb0",
  "#b0667f",
  "#3f7d6b",
  "#5b6ea8",
  "#4a8fa3",
  "#7a7f87",
  "#b07a4f",
  "#8b6ba8",
  "#5f9e76",
];

const CHART_LINE_STYLES = ["", "4 4", "2 4", "6 3"];
const EMPTY_ACTIVITY_ROWS: ActivityRow[] = [];
const EMPTY_ACTIVITY_COVERAGE: ActivityCoverageRow[] = [];
const EMPTY_DAY_TOTALS: Readonly<Record<string, number>> = {};

function dayKey(d: Date): string {
  return localDayKey(d);
}

type CalendarDay = {
  day: string;
  inRange: boolean;
  weekday: number;
  week: number;
  total: number;
  state: "missing" | "partial" | "zero" | "active";
  level: number;
};

function buildCalendar(
  from: string,
  to: string,
  activity: ActivityRow[],
  coverage: ActivityCoverageRow[],
  dayTotals: Readonly<Record<string, number>>,
): {
  days: CalendarDay[];
  weeks: number;
  max: number;
  monthAnchors: { label: string; offset: number }[];
} {
  const partialDays = new Set<string>();
  for (const r of activity) {
    const day = r.day ?? "unknown";
    if (day === "unknown") continue;
    const quality = (r.quality ?? "").toLowerCase();
    if (quality && quality !== "direct" && quality !== "exact") {
      partialDays.add(day);
    }
  }

  const coverageByDay = new Map<string, { covered: number; rows: number }>();
  for (const c of coverage) {
    const day = c.day ?? "unknown";
    if (day === "unknown") continue;
    const entry = coverageByDay.get(day) ?? { covered: 0, rows: 0 };
    entry.rows += 1;
    if (c.covered) entry.covered += 1;
    coverageByDay.set(day, entry);
  }

  const start = startOfWeek(from);
  const end = endOfWeek(to);
  const allDays = eachDay(start, end);
  const weeks = Math.max(1, Math.ceil(allDays.length / 7));
  const inRangeSet = new Set(eachDay(from, to).map(dayKey));

  const max = Math.max(
    1,
    ...Object.entries(dayTotals)
      .filter(([d]) => inRangeSet.has(d))
      .map(([, v]) => v),
  );

  const calendarDays: CalendarDay[] = allDays.map((d, index) => {
    const day = dayKey(d);
    const inRange = inRangeSet.has(day);
    const total = inRange ? (dayTotals[day] ?? 0) : 0;
    let state: CalendarDay["state"] = "missing";
    let level = 0;
    const coverage = coverageByDay.get(day);
    if (!inRange) {
      state = "missing";
    } else if (dayTotals[day] !== undefined) {
      if (
        partialDays.has(day) ||
        (coverage !== undefined && coverage.covered < coverage.rows)
      ) {
        state = "partial";
      } else if (total === 0) {
        state = "zero";
      } else {
        state = "active";
        level = Math.max(1, Math.min(5, Math.ceil((total / max) * 5)));
      }
    } else if (coverage && coverage.rows > 0) {
      state = coverage.covered === coverage.rows ? "zero" : "partial";
    } else {
      state = "missing";
    }
    return {
      day,
      inRange,
      weekday: d.getDay(),
      week: Math.floor(index / 7),
      total,
      state,
      level,
    };
  });

  const monthAnchors: { label: string; offset: number }[] = [];
  const seen = new Set<string>();
  for (const [index, d] of allDays.entries()) {
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    if (!seen.has(key)) {
      seen.add(key);
      const week = Math.floor(index / 7);
      monthAnchors.push({
        label: d.toLocaleString(navigator.language, {
          month: "short",
        }),
        offset: week * (HEATMAP_CELL_SIZE + HEATMAP_GAP),
      });
    }
  }

  return { days: calendarDays, weeks, max, monthAnchors };
}

type TooltipPayloadItem = {
  name?: string | number;
  value?: string | number | readonly (string | number)[];
  color?: string;
};

function modelTooltipContent({
  active,
  payload,
  label,
  t,
  dayTotals,
}: {
  active?: boolean;
  payload?: ReadonlyArray<TooltipPayloadItem>;
  label?: string | number;
  t: Messages;
  dayTotals?: Record<string, number>;
}) {
  if (!active || !payload || !payload.length || !label) return null;
  const items = payload
    .map((p) => {
      const raw = Array.isArray(p.value) ? p.value[0] : p.value;
      return {
        name: String(p.name ?? ""),
        value: Number(raw) || 0,
        color: p.color ?? "var(--text-dim)",
      };
    })
    // Defensively exclude a "total" series if one is ever added to the chart.
    .filter(
      (p) => p.value > 0 && String(p.name ?? "") !== "total",
    );
  // The chart only plots the top 12 models, so summing the payload would use
  // partial data. Prefer the true daily total computed from the full row set.
  const total =
    dayTotals?.[String(label)] ?? items.reduce((sum, p) => sum + p.value, 0);
  return (
    <div className="model-chart-tip">
      <div className="model-chart-tip-row">
        <span className="model-chart-tip-label">{String(label)}</span>
        <span className="model-chart-tip-value">{formatTokenCompact(total)}</span>
      </div>
      {items.map((p) => (
        <div className="model-chart-tip-row" key={p.name}>
          <span className="model-chart-tip-label">
            <span
              className="model-chart-tip-dot"
              style={{ background: p.color }}
            />
            {p.name}
          </span>
          <span className="model-chart-tip-value">{formatTokenCompact(p.value)}</span>
        </div>
      ))}
      <div className="model-chart-tip-row model-chart-tip-total">
        <span className="model-chart-tip-label">{t.totalTokens}</span>
        <span className="model-chart-tip-value">{formatTokenCompact(total)}</span>
      </div>
    </div>
  );
}

export default function ActivityPage({
  t,
  refreshNonce = 0,
}: {
  t: Messages;
  refreshNonce?: number;
}) {
  const [period, setPeriod] = useState<Period>("month");
  const [snapshot, setSnapshot] = useState<Snapshot>({});
  const [activitySnapshot, setActivitySnapshot] =
    useState<LoadedActivitySnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [activityLoading, setActivityLoading] = useState(true);
  const [providerFilter, setProviderFilter] = useState<string>("all");
  const [modelFilters, setModelFilters] = useState<ReadonlySet<string>>(new Set());
  const [modelPanelOpen, setModelPanelOpen] = useState(false);
  const [selectedHeatDay, setSelectedHeatDay] = useState<string | null>(null);
  const [hoveredHeatDay, setHoveredHeatDay] = useState<string | null>(null);
  const [hiddenModels, setHiddenModels] = useState<Set<string>>(new Set());
  const [providerDisplay, setProviderDisplay] = useState<Map<string, string>>(new Map());
  const [showAllModels, setShowAllModels] = useState(false);
  const latestActivityRequest = useRef(0);

  const snapshotRangeEnd = activitySnapshot?.range.to;
  const { from, to } = useMemo(
    () =>
      rangeFor(
        periodDays(period),
        snapshotRangeEnd === undefined ? new Date() : parseDayKey(snapshotRangeEnd),
      ),
    [period, snapshotRangeEnd],
  );
  const activity = activitySnapshot?.response.rows ?? EMPTY_ACTIVITY_ROWS;
  const derivedActivity = useMemo(
    () =>
      activitySnapshot
        ? deriveActivitySnapshot(activitySnapshot.response, { from, to }, {
            providerId: providerFilter === "all" ? undefined : providerFilter,
            modelIds: modelFilters,
          })
        : null,
    [activitySnapshot, from, to, providerFilter, modelFilters],
  );

  useEffect(() => {
    let active = true;
    let inFlight = false;
    async function refreshSnapshot() {
      if (inFlight) return;
      inFlight = true;
      try {
        const next = await fetchSnapshot();
        if (!active) return;
        setSnapshot(next);
        setSnapshotError(null);
      } catch (e) {
        if (active) {
          setSnapshotError(e instanceof Error ? e.message : "failed");
        }
      } finally {
        inFlight = false;
      }
    }

    void refreshSnapshot();
    // Keep the summary metrics (今日 Token / Provider / 覆盖天数) live while
    // the page is open: the collector imports new data every few minutes.
    const timer = setInterval(() => {
      void refreshSnapshot();
    }, 60_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [refreshNonce]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchProviders()
      .then((providers) => {
        if (controller.signal.aborted) return;
        const map = new Map<string, string>();
        for (const p of providers) {
          const id = p.providerId ?? p.familyId ?? "";
          if (id && p.displayName) map.set(id, p.displayName);
        }
        setProviderDisplay(map);
      })
      .catch(() => {
        // Display names are cosmetic; fall back to provider IDs.
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    let active = true;
    let controller: AbortController | null = null;
    setActivityLoading(!shouldPreserveActivitySnapshot(activitySnapshot));

    async function refreshActivity() {
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const requestId = ++latestActivityRequest.current;
      const range = rangeFor(365);
      const scopeKey = `${range.from}:${range.to}:all`;
      try {
        const response = await fetchActivity(
          range.from,
          range.to,
          undefined,
          undefined,
          signal,
        );
        if (!active || signal.aborted || requestId !== latestActivityRequest.current) return;
        const candidate = { scopeKey, range, response };
        setActivitySnapshot((current) =>
          shouldReplaceActivitySnapshot(current, candidate) ? candidate : current,
        );
        setActivityError(null);
      } catch (e) {
        if (active && !signal.aborted && requestId === latestActivityRequest.current) {
          setActivityError(e instanceof Error ? e.message : "failed");
        }
      } finally {
        if (active && !signal.aborted && requestId === latestActivityRequest.current) {
          setActivityLoading(false);
        }
      }
    }

    void refreshActivity();
    const timer = setInterval(() => void refreshActivity(), 60_000);
    return () => {
      active = false;
      clearInterval(timer);
      controller?.abort();
    };
  }, [refreshNonce]);

  useEffect(() => {
    setSelectedHeatDay(null);
    setHoveredHeatDay(null);
  }, [providerFilter, modelFilters]);

  useEffect(() => {
    if (activitySnapshot === null) return;
    if (
      providerFilter !== "all" &&
      !activity.some((r) => r.providerId === providerFilter)
    ) {
      setProviderFilter("all");
    }
  }, [activitySnapshot, activity, providerFilter]);

  useEffect(() => {
    if (modelFilters.size === 0) return;
    const present = new Set(activity.map((r) => r.modelId ?? "").filter(Boolean));
    const stale = [...modelFilters].filter((m) => !present.has(m));
    if (stale.length > 0) {
      setModelFilters((prev) => {
        const next = new Set(prev);
        for (const m of stale) next.delete(m);
        return next;
      });
    }
  }, [activity, modelFilters]);

  const allProviders = useMemo(
    () =>
      Array.from(
        new Set(activity.map((r) => r.providerId ?? "").filter(Boolean)),
      ).sort(),
    [activity],
  );
  const groupedModels = useMemo(() => {
    const groups = new Map<string, string[]>();
    for (const r of activity) {
      const provider = r.providerId ?? "unknown";
      if (providerFilter !== "all" && provider !== providerFilter) continue;
      const model = r.modelId ?? "unknown";
      if (!model) continue;
      const list = groups.get(provider) ?? [];
      if (!list.includes(model)) list.push(model);
      groups.set(provider, list);
    }
    return [...groups.entries()]
      .map(([provider, models]) => ({ provider, models: models.sort() }))
      .sort((a, b) => a.provider.localeCompare(b.provider));
  }, [activity, providerFilter]);

  const modelProviders = useMemo(() => {
    if (modelFilters.size === 0) return allProviders;
    const selected = new Set(modelFilters);
    return Array.from(
      new Set(
        activity
          .filter((r) => r.modelId && selected.has(r.modelId))
          .map((r) => r.providerId ?? ""),
      ),
    ).sort();
  }, [activity, modelFilters, allProviders]);


  function providerLabel(id: string): string {
    return providerDisplay.get(id) ?? id;
  }

  function toggleModel(model: string) {
    setModelFilters((prev) => {
      const next = new Set(prev);
      if (next.has(model)) next.delete(model);
      else next.add(model);
      return next;
    });
  }

  const filtered = derivedActivity?.periodRows ?? EMPTY_ACTIVITY_ROWS;
  const heatmapFiltered = derivedActivity?.rows ?? EMPTY_ACTIVITY_ROWS;
  const heatmapCoverage = derivedActivity?.coverage ?? EMPTY_ACTIVITY_COVERAGE;
  const periodCoverage = derivedActivity?.periodCoverage ?? EMPTY_ACTIVITY_COVERAGE;
  const dayTotals = derivedActivity?.periodDayTotals ?? EMPTY_DAY_TOTALS;
  const heatmapDayTotals = derivedActivity?.dayTotals ?? EMPTY_DAY_TOTALS;

  const totals = useMemo(() => {
    const byDay: Record<string, number> = { ...dayTotals };
    const rowDays = new Set<string>();
    const partialDays = new Set<string>();
    const coverageByDay = new Map<string, { covered: number; rows: number }>();
    const total = derivedActivity?.periodTotal ?? 0;
    let input = 0;
    let output = 0;
    let cacheRead = 0;
    let cacheCreation = 0;
    let reasoning = 0;
    for (const r of filtered) {
      input += r.inputTokens ?? 0;
      output += r.outputTokens ?? 0;
      cacheRead += r.cacheReadTokens ?? 0;
      cacheCreation += r.cacheCreationTokens ?? 0;
      if (r.reasoningTokens) reasoning += r.reasoningTokens;
      if (r.day === undefined) continue;
      rowDays.add(r.day);
      const quality = (r.quality ?? "").toLowerCase();
      if (quality && quality !== "direct" && quality !== "exact") {
        partialDays.add(r.day);
      }
    }
    for (const item of periodCoverage) {
      if (item.day === undefined) continue;
      const entry = coverageByDay.get(item.day) ?? { covered: 0, rows: 0 };
      entry.rows += 1;
      if (item.covered) entry.covered += 1;
      coverageByDay.set(item.day, entry);
    }
    const days = eachDay(from, to).map(dayKey);
    for (const day of days) byDay[day] ??= 0;
    const active = days.filter((d) => byDay[d] > 0).length;
    let peakDay = "—";
    let peakValue = 0;
    for (const d of days) {
      if (byDay[d] > peakValue) {
        peakValue = byDay[d];
        peakDay = d;
      }
    }

    let currentStreak = 0;
    for (let i = days.length - 1; i >= 0; i--) {
      if (byDay[days[i]] > 0) currentStreak++;
      else break;
    }

    let longestStreak = 0;
    let run = 0;
    for (const d of days) {
      if (byDay[d] > 0) {
        run++;
        longestStreak = Math.max(longestStreak, run);
      } else {
        run = 0;
      }
    }

    const rangeDays = rangeLength(from, to);
    let completeDays = 0;
    let hasObservedData = rowDays.size > 0;
    for (const day of days) {
      const coverage = coverageByDay.get(day);
      if ((coverage?.covered ?? 0) > 0) hasObservedData = true;
      if (rowDays.has(day)) {
        if (
          !partialDays.has(day) &&
          (coverage === undefined || coverage.covered === coverage.rows)
        ) {
          completeDays += 1;
        }
      } else if (coverage && coverage.rows > 0 && coverage.covered === coverage.rows) {
        completeDays += 1;
      }
    }
    const isComplete = completeDays === rangeDays;
    const isMissing = !hasObservedData;
    return {
      total,
      input,
      output,
      cacheRead,
      cacheCreation,
      reasoning,
      active,
      peakDay,
      peakValue,
      currentStreak,
      longestStreak,
      isComplete,
      isMissing,
      rangeDays,
    };
  }, [dayTotals, derivedActivity, filtered, periodCoverage, from, to]);

  const topModels = useMemo(() => {
    const modelTotals = new Map<string, number>();
    for (const r of filtered) {
      const model = r.modelId ?? "unknown";
      modelTotals.set(model, (modelTotals.get(model) ?? 0) + (r.totalTokens ?? 0));
    }
    return [...modelTotals.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12)
      .map(([m]) => m);
  }, [filtered]);

  const visibleModels = useMemo(
    () => topModels.filter((m) => !hiddenModels.has(m)),
    [topModels, hiddenModels],
  );

  const modelChartData = useMemo(() => {
    const byDay = new Map<string, Record<string, number>>();
    for (const r of filtered) {
      const model = r.modelId ?? "unknown";
      if (!topModels.includes(model)) continue;
      const day = r.day ?? "unknown";
      const entry = byDay.get(day) ?? {};
      entry[model] = (entry[model] ?? 0) + (r.totalTokens ?? 0);
      byDay.set(day, entry);
    }
    return eachDay(from, to).map((d) => {
      const day = dayKey(d);
      const point: Record<string, number | string> = { day };
      for (const m of topModels) point[m] = byDay.get(day)?.[m] ?? 0;
      return point;
    });
  }, [filtered, topModels, from, to]);

  const fallbackHeatRange = useMemo(() => rangeFor(365), []);
  const heatRange = activitySnapshot?.range ?? fallbackHeatRange;
  const { days: calendarDays, weeks, monthAnchors } = useMemo(
    () =>
      buildCalendar(
        heatRange.from,
        heatRange.to,
        heatmapFiltered,
        heatmapCoverage,
        heatmapDayTotals,
      ),
    [heatRange, heatmapFiltered, heatmapCoverage, heatmapDayTotals],
  );

  const heatStats = useMemo(() => {
    const inRange = calendarDays.filter((d) => d.inRange);
    const active = inRange.filter((d) => d.total > 0).length;
    const missing = inRange.filter((d) => d.state === "missing").length;
    const partial = inRange.filter((d) => d.state === "partial").length;
    const total = inRange.reduce((sum, d) => sum + d.total, 0);
    return {
      active,
      missing,
      partial,
      total,
      rangeDays: inRange.length,
    };
  }, [calendarDays]);

  const dayNames = useMemo(() => {
    const fmt = new Intl.DateTimeFormat(navigator.language, { weekday: "narrow" });
    return Array.from({ length: 7 }, (_, i) => fmt.format(new Date(2024, 0, 7 + i)));
  }, []);

  const inRangeHeatDays = useMemo(
    () => calendarDays.filter((d) => d.inRange).map((d) => d.day).sort(),
    [calendarDays],
  );
  const heatAnchor =
    selectedHeatDay ?? hoveredHeatDay ?? inRangeHeatDays[inRangeHeatDays.length - 1] ?? null;

  function moveHeatFocus(next: string | null) {
    if (!next) return;
    setHoveredHeatDay(next);
    requestAnimationFrame(() => {
      document.getElementById(`heat-${next}`)?.focus();
    });
  }

  function handleHeatKey(event: React.KeyboardEvent<HTMLDivElement>, day: string) {
    const index = inRangeHeatDays.indexOf(day);
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[Math.max(0, index - 1)] ?? null);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[Math.min(inRangeHeatDays.length - 1, index + 1)] ?? null);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[Math.max(0, index - 7)] ?? null);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[Math.min(inRangeHeatDays.length - 1, index + 7)] ?? null);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[0] ?? null);
    } else if (event.key === "End") {
      event.preventDefault();
      moveHeatFocus(inRangeHeatDays[inRangeHeatDays.length - 1] ?? null);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedHeatDay((prev) => (prev === day ? null : day));
      setHoveredHeatDay(day);
    }
  }

  const summary = snapshot.summary ?? {};
  const quotas = snapshot.quotaHub ?? [];
  const providers = snapshot.providers ?? [];
  const tokens = useAnimatedNumber(summary.todayTokens ?? 0);
  const tokenValue =
    summary.todayTokens === undefined
      ? "—"
      : Math.round(tokens).toLocaleString();
  const tokenMeta =
    summary.modelCount !== undefined
      ? `${summary.modelCount} ${t.providers} · ${summary.coveredDayCount ?? 0} ${t.daysLabel}`
      : undefined;

  const heatBadge = totals.isMissing
    ? { state: "missing" as const, text: t.missing }
    : !totals.isComplete
      ? { state: "partial" as const, text: t.partial }
      : undefined;

  const activeHeatDay = selectedHeatDay ?? hoveredHeatDay ?? null;
  const heatSummary = useMemo(() => {
    if (!activeHeatDay) return null;
    const day = calendarDays.find((d) => d.day === activeHeatDay);
    if (!day) return null;
    const date = parseDayKey(day.day).toLocaleDateString(navigator.language, {
      month: "short",
      day: "numeric",
    });
    if (day.state === "missing") {
      return { date, text: t.noData, value: "" };
    }
    if (day.state === "partial") {
      return {
        date,
        text: t.heatmapLegendPartial,
        value: day.total > 0 ? formatTokenCompact(day.total) : "",
      };
    }
    if (day.state === "zero") {
      return { date, text: t.activeDays, value: "0" };
    }
    return {
      date,
      text: `${t.totalTokens}`,
      value: formatTokenCompact(day.total),
    };
  }, [activeHeatDay, calendarDays, t]);
  const error = activityError ?? snapshotError;

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} t={t} />
      {activityLoading ? (
        <Skeleton lines={8} />
      ) : (
        <>
          <div className="metrics">
            <KpiV2
              label={t.todayTokens}
              value={tokenValue}
              meta={tokenMeta}
            />
            <KpiV2
              label={t.providers}
              value={String(providers.length)}
            />
            <KpiV2
              label={t.balance}
              value={String(quotas.length)}
              meta={quotas.map((q) => q.currency).filter(Boolean).join(", ")}
            />
          </div>

          <div className="filter-bar">
            <Funnel size={16} aria-hidden="true" />
            <select
              className="filter-select"
              value={providerFilter}
              onChange={(e) => setProviderFilter(e.target.value)}
              aria-label={t.providerFilter}
            >
              <option value="all">{t.allProviders}</option>
              {modelProviders.map((p) => (
                <option key={p} value={p}>
                  {providerLabel(p)}
                </option>
              ))}
            </select>
            <div className="model-multiselect">
              <button
                type="button"
                className="filter-select filter-multiselect-trigger"
                onClick={() => setModelPanelOpen((v) => !v)}
                aria-expanded={modelPanelOpen}
                aria-haspopup="listbox"
              >
                {modelFilters.size === 0
                  ? t.allModels
                  : tpl(t.modelsSelected, { count: String(modelFilters.size) })}
              </button>
              {modelPanelOpen ? (
                <div className="model-multiselect-panel" role="listbox" aria-multiselectable="true">
                  <div className="model-multiselect-panel-head">
                    <span>{t.selectModels}</span>
                    <button
                      type="button"
                      className="btn-link"
                      onClick={() => {
                        setModelFilters(new Set());
                        setModelPanelOpen(false);
                      }}
                    >
                      {t.clearModels}
                    </button>
                  </div>
                  {groupedModels.length === 0 ? (
                    <p className="empty">{t.noRows}</p>
                  ) : (
                    groupedModels.map((group) => (
                      <div className="model-group" key={group.provider}>
                        <div className="model-group-name">{providerLabel(group.provider)}</div>
                        <div className="model-group-items">
                          {group.models.map((m) => {
                            const checked = modelFilters.has(m);
                            return (
                              <label className="model-check" key={m}>
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => toggleModel(m)}
                                />
                                <span>{m}</span>
                              </label>
                            );
                          })}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              ) : null}
            </div>
            {(providerFilter !== "all" || modelFilters.size > 0) && (
              <button
                type="button"
                className="icon-btn"
                onClick={() => {
                  setProviderFilter("all");
                  setModelFilters(new Set());
                  setModelPanelOpen(false);
                }}
              >
                {t.resetFilters}
              </button>
            )}
          </div>

          {error ? <p className="empty">{error}</p> : null}

          <div className="metrics">
            <KpiV2
              label={totals.isComplete ? t.totalTokens : t.observedTokens}
              value={formatTokenCompact(totals.total)}
              badge={heatBadge}
            />
            <KpiV2
              label={t.inputTokens}
              value={formatTokenCompact(totals.input)}
            />
            <KpiV2
              label={t.outputTokens}
              value={formatTokenCompact(totals.output)}
            />
            {(totals.cacheRead > 0 || totals.cacheCreation > 0) && (
              <>
                <KpiV2
                  label={t.cacheReadTokens}
                  value={formatTokenCompact(totals.cacheRead)}
                />
                <KpiV2
                  label={t.cacheCreationTokens}
                  value={formatTokenCompact(totals.cacheCreation)}
                />
              </>
            )}
            {totals.reasoning > 0 && (
              <KpiV2
                label={t.reasoningTokens}
                value={formatTokenCompact(totals.reasoning)}
              />
            )}
          </div>

          <section className="panel" aria-label={t.modelChartTitle}>
            <div className="panel-head">
              <h3>{t.modelChartTitle}</h3>
              <span>{t.modelChartHint}</span>
            </div>
            <div className="panel-body">
              {modelChartData.length > 0 && visibleModels.length > 0 ? (
                <>
                  <div className="model-chart">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={modelChartData}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis
                          dataKey="day"
                          tickFormatter={(d: string) => String(d).slice(5)}
                        />
                        <YAxis tickFormatter={(v: number) => formatTokenCompact(v)} width={70} />
                        <Tooltip
                          content={(props) => modelTooltipContent({ ...props, t, dayTotals })}
                        />
                        {visibleModels.map((m, i) => (
                          <Line
                            key={m}
                            type="monotone"
                            dataKey={m}
                            stroke={MODEL_COLORS[i % MODEL_COLORS.length]}
                            strokeWidth={2}
                            strokeDasharray={CHART_LINE_STYLES[i % CHART_LINE_STYLES.length] || undefined}
                            dot={false}
                            isAnimationActive={false}
                          />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="model-chart-legend">
                    {topModels.map((m, i) => {
                      const hidden = hiddenModels.has(m);
                      return (
                        <button
                          key={m}
                          type="button"
                          className={`model-chart-legend-item${hidden ? " hidden" : ""}`}
                          onClick={() =>
                            setHiddenModels((prev) => {
                              const next = new Set(prev);
                              if (next.has(m)) next.delete(m);
                              else next.add(m);
                              return next;
                            })
                          }
                          aria-pressed={!hidden}
                        >
                          <span
                            className="model-chart-legend-dot"
                            style={{
                              background: MODEL_COLORS[i % MODEL_COLORS.length],
                              opacity: hidden ? 0.35 : 1,
                            }}
                          />
                          <span style={{ textDecoration: hidden ? "line-through" : undefined }}>
                            {m}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : (
                <p className="empty">{t.noModelTrend}</p>
              )}
            </div>
          </section>

          <section className="panel heatmap-panel" aria-label={t.heatmapTitle}>
            <div className="panel-head">
              <h3>{t.heatmapTitle}</h3>
              <span>{t.heatmapSubtitle}</span>
            </div>
            <div className="panel-body">
              {calendarDays.length > 0 ? (
                <>
                  <div className="heatmap-v2-summary">
                    <span>
                      {heatSummary
                        ? `${heatSummary.date} · ${heatSummary.text} ${heatSummary.value}`
                        : `${heatStats.active} / ${heatStats.rangeDays} ${t.activeDays} · ${formatTokenCompact(heatStats.total)} ${t.totalTokens}`}
                    </span>
                    <div className="heatmap-v2-legend">
                      <span className="heatmap-legend-item">
                        <span className="heatmap-v2-legend-box heatmap-v2-cell missing" />
                        <span>{t.heatmapLegendMissing}</span>
                      </span>
                      <span className="heatmap-legend-item">
                        <span className="heatmap-v2-legend-box heatmap-v2-cell partial" />
                        <span>{t.heatmapLegendPartial}</span>
                      </span>
                      <span className="heatmap-legend-item">
                        <span className="heatmap-v2-legend-box heatmap-v2-cell zero" />
                        <span>{t.heatmapLegendZero}</span>
                      </span>
                      <span className="heatmap-legend-scale">
                        <span>{t.heatmapLegendLower}</span>
                        {[1, 2, 3, 4, 5].map((l) => (
                          <span
                            key={l}
                            className={`heatmap-v2-legend-box heatmap-v2-cell level-${l}`}
                          />
                        ))}
                        <span>{t.heatmapLegendHigher}</span>
                      </span>
                    </div>
                  </div>
                  <div className="heatmap-scroll">
                    <div className="heatmap-wrap">
                      <div
                        className="heatmap-months"
                        style={{
                          width: weeks * (HEATMAP_CELL_SIZE + HEATMAP_GAP),
                        }}
                      >
                        {monthAnchors.map((m) => (
                          <span
                            key={m.label + m.offset}
                            className="heatmap-month-label"
                            style={{ left: m.offset }}
                          >
                            {m.label}
                          </span>
                        ))}
                      </div>
                      <div className="heatmap-rows">
                        {Array.from({ length: 7 }, (_, weekday) => {
                          const rowDays = calendarDays.filter(
                            (d) => d.weekday === weekday,
                          );
                          return (
                            <div key={weekday} className="heatmap-v2-row">
                              <div className="heatmap-v2-day-label">
                                {weekday === 0 || weekday === 6 ? dayNames[weekday] : ""}
                              </div>
                              <div className="heatmap-v2-cells">
                                {rowDays.map((d) => (
                                  <div
                                    id={`heat-${d.day}`}
                                    key={d.day}
                                    className={`heatmap-v2-cell${
                                      d.inRange ? "" : " missing"
                                    }${d.state === "partial" ? " partial" : ""}${
                                      d.state === "zero" ? " zero" : ""
                                    }${
                                      d.state === "active" ? ` level-${d.level}` : ""
                                    }${selectedHeatDay === d.day ? " selected" : ""}`}
                                    onMouseEnter={() => setHoveredHeatDay(d.day)}
                                    onMouseLeave={() => setHoveredHeatDay(null)}
                                    onClick={() =>
                                      setSelectedHeatDay((prev) =>
                                        prev === d.day ? null : d.day,
                                      )
                                    }
                                    onKeyDown={(event) => handleHeatKey(event, d.day)}
                                    title={`${d.day}: ${formatTokenCompact(d.total)}`}
                                    role="button"
                                    aria-label={`${d.day}: ${formatTokenCompact(d.total)}`}
                                    tabIndex={d.inRange && d.day === heatAnchor ? 0 : -1}
                                  />
                                ))}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </>
              ) : (
                <p className="empty">{t.noFilteredActivity}</p>
              )}
            </div>
          </section>

          <h2 className="section-title">
            {t.quotaHub} <span className="dim">· {t.freeQuotaAggregation}</span>
          </h2>
          <div className="quota-grid">
            {quotas.map((item, index) => (
              <article className="quota" key={index}>
                <div className="quota-bar" aria-hidden="true" />
                <p className="quota-currency">{item.currency ?? "n/a"}</p>
                <p className="quota-amount">{item.totalAvailable ?? "n/a"}</p>
                <p className="quota-meta">
                  {item.providerCount ?? 0} {t.providers}
                </p>
                {item.provenance && item.provenance.length > 0 ? (
                  <ul className="provenance">
                    {item.provenance.map((part, i) => (
                      <li className="mono" key={i}>
                        {part.join(":")}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))}
            {quotas.length === 0 ? <p className="empty">{t.noSpend}</p> : null}
          </div>

          <section className="panel" aria-label={t.modelSpend}>
            <div className="panel-head">
              <h3>{t.modelSpend}</h3>
              <span>
                {filtered.length > 20 ? (
                  <button
                    type="button"
                    className="text-btn"
                    onClick={() => setShowAllModels((v) => !v)}
                  >
                    {showAllModels ? t.showLess : t.showAll}
                  </button>
                ) : null}
                {t[PERIOD_LABEL[period]]}
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.providerCol}</th>
                    <th scope="col">{t.modelCol}</th>
                    <th scope="col">{t.tokensCol}</th>
                    <th scope="col">{t.sourceCol}</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, showAllModels ? undefined : 20).map((item) => (
                    <tr
                      key={`${item.providerId}-${item.modelId}-${item.sourceId}-${item.day}`}
                    >
                      <th scope="row">{item.providerId ?? "n/a"}</th>
                      <td className="mono">{item.modelId ?? "n/a"}</td>
                      <td className="mono">
                        {(item.totalTokens ?? 0).toLocaleString()}
                      </td>
                      <td className="mono dim">{item.sourceId ?? "n/a"}</td>
                    </tr>
                  ))}
                  {filtered.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="empty">
                        {t.noFilteredActivity}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </>
  );
}
