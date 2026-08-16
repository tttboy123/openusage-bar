import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ChartLine } from "@phosphor-icons/react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import { fetchCapacity, fetchQuotaHistory, type CapacityProvider, type QuotaHistoryItem } from "../api";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";

function ratioWidth(ratio: number | undefined): string {
  if (ratio === undefined) return "0%";
  const clamped = Math.max(0, Math.min(1, ratio)) * 100;
  return `${clamped}%`;
}

function kindLabel(kind: string | undefined, t: Messages): string {
  if (kind === "subscription") return t.subscription;
  if (kind === "account") return t.account;
  if (kind === "api") return t.apiPaid;
  return kind ?? "—";
}

function capacityState(state: string | undefined): "ok" | "warn" | "bad" {
  const value = (state ?? "").toLowerCase();
  if (value === "ok" || value === "available" || value === "active") return "ok";
  if (value === "warn" || value === "low" || value === "stale" || value === "temporarily_unavailable") return "warn";
  return "bad";
}

function capacityStateLabel(state: string | undefined, t: Messages): string {
  const kind = capacityState(state);
  if (kind === "ok") return t.capacityOk;
  if (kind === "warn") return t.capacityWarn;
  return t.capacityBad;
}

const HISTORY_COLORS = [
  "#4f6f8f", "#6b8f71", "#b38b4f", "#8a6fb0", "#b0667f", "#3f7d6b",
  "#5b6ea8", "#4a8fa3", "#7a7f87", "#b07a4f",
];

function groupKey(item: QuotaHistoryItem): string {
  return `${item.providerId ?? "unknown"}:${item.quotaName ?? "default"}:${item.accountRef ?? ""}`;
}

function groupLabel(item: QuotaHistoryItem): string {
  const parts = [item.providerId, item.quotaName];
  if (item.accountRef) parts.push(item.accountRef);
  return parts.filter(Boolean).join(" · ");
}

export default function CapacityPage({ t }: { t: Messages }) {
  const [items, setItems] = useState<CapacityProvider[]>([]);
const [history, setHistory] = useState<QuotaHistoryItem[]>([]);
const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetchCapacity(),
      fetchQuotaHistory(undefined, undefined, undefined, undefined, 1000),
    ])
      .then(([capacity, hist]) => {
        setItems(capacity);
        setHistory(hist);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"))
      .finally(() => setLoading(false));
  }, []);

  const groups = useMemo(() => {
    const map = new Map<string, QuotaHistoryItem[]>();
    for (const item of history) {
      const key = groupKey(item);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(item);
    }
    for (const list of map.values()) list.sort((a, b) => (a.observedAt ?? "").localeCompare(b.observedAt ?? ""));
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [history]);

  const visibleGroups = useMemo(
    () => (showAll ? groups : groups.slice(0, 6)),
    [groups, showAll],
  );

  const chartData = useMemo(() => {
    const atSet = new Set<string>();
    const byKey = new Map<string, Map<string, QuotaHistoryItem>>();
    for (const [key, list] of visibleGroups) {
      const inner = new Map<string, QuotaHistoryItem>();
      for (const item of list) {
        const at = item.observedAt ?? "";
        atSet.add(at);
        inner.set(at, item);
      }
      byKey.set(key, inner);
    }
    const ats = Array.from(atSet).sort();
    return ats.map((at) => {
      const point: Record<string, number | string | null> = { at };
      for (const [key] of visibleGroups) {
        const item = byKey.get(key)?.get(at);
        point[key] = item?.remainingRatio ?? null;
      }
      return point;
    });
  }, [visibleGroups]);

  const okCount = items.filter((item) => capacityState(item.state) === "ok").length;
  const warnCount = items.filter((item) => capacityState(item.state) === "warn").length;
  const badCount = items.filter((item) => capacityState(item.state) === "bad").length;
  const ratios = items
    .map((item) => item.remainingRatio)
    .filter((r): r is number => r !== undefined);
  const avgRemaining =
    ratios.length > 0
      ? Math.round((ratios.reduce((a, b) => a + b, 0) / ratios.length) * 100)
      : null;

  if (loading) return <Skeleton lines={5} />;

  return (
    <>
      <div className="metrics">
        <div className="metric">
          <p className="metric-value">{items.length}</p>
          <p className="metric-label">{t.scopes}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{okCount}</p>
          <p className="metric-label">{t.capacityOk}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{warnCount}</p>
          <p className="metric-label">{t.capacityWarn}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{badCount}</p>
          <p className="metric-label">{t.capacityBad}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{avgRemaining === null ? "—" : `${avgRemaining}%`}</p>
          <p className="metric-label">{t.remainingCol}</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.navCapacity}</h3>
          <span>{items.length} {t.scopes}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">{t.providerCol}</th>
                <th scope="col">{t.categoryCol}</th>
                <th scope="col">{t.quotaCol}</th>
                <th scope="col">{t.usedCol}</th>
                <th scope="col">{t.remainingCol}</th>
                <th scope="col">{t.stateCol}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${item.providerId}-${index}`}>
                  <th scope="row">{item.providerId ?? "n/a"}</th>
                  <td>{kindLabel(item.appliesTo?.kind, t)}</td>
                  <td>{item.quotaName ?? "n/a"}</td>
                  <td className="mono">
                    {item.used ?? "n/a"}
                    {item.unit === "percent" ? "%" : ""}
                  </td>
                  <td className="mono">
                    {item.remaining ?? "n/a"}
                    {item.remainingRatio !== undefined ? (
                      <span
                        className="remaining-track"
                        aria-hidden="true"
                      >
                        <span
                          className={`remaining-fill${
                            (item.remainingRatio ?? 0) < 0.1
                              ? " remaining-fill-bad"
                              : (item.remainingRatio ?? 0) < 0.2
                                ? " remaining-fill-warn"
                                : ""
                          }`}
                          style={{ width: ratioWidth(item.remainingRatio) }}
                        />
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <span className={`pill pill-${capacityState(item.state)}`}>
                      {capacityStateLabel(item.state, t)}
                    </span>
                  </td>
                </tr>
              ))}
              {items.length === 0 && !error ? (
                <tr>
                  <td colSpan={6} className="empty">
                    {t.comingSoon}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
        {error ? <p className="empty">{error}</p> : null}
      </section>

      <section className="panel" aria-label={t.quotaHistory}>
        <div className="panel-head">
          <h3>
            <ChartLine size={16} style={{ marginRight: 6, verticalAlign: -2 }} />
            {t.quotaHistory}
          </h3>
          <div className="focus-toggle">
            {groups.length > 6 ? (
              <button type="button" onClick={() => setShowAll((v) => !v)}>
                {!showAll ? t.focusTop.replace("{count}", "6") : t.showAll}
              </button>
            ) : null}
            <span>
              {visibleGroups.length} / {groups.length} {t.scopes}
            </span>
          </div>
        </div>
         <div className="panel-body">
            {groups.length > 0 ? (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                   <XAxis
                     dataKey="at"
                    tickFormatter={(at: string | number) => (at ? String(at).slice(0, 10) : "")}
                     minTickGap={30}
                   />
                   <YAxis domain={[0, 1]} tickFormatter={(v: number) => `${Math.round(Number(v) * 100)}%`} />
                   <Tooltip
                    formatter={(value: number | string | readonly (number | string)[] | undefined, name: number | string | undefined) => [
                      value != null ? `${Math.round(Number(value) * 100)}%` : "—",
                      groupLabel(history.find((h) => groupKey(h) === name) ?? {}),
                    ]}
                    labelFormatter={(label: ReactNode) => String(label).slice(0, 16).replace("T", " ")}
                   />
                  <Legend />
                  {visibleGroups.map(([key], i) => (
                    <Line
                      key={key}
                      type="monotone"
                      dataKey={key}
                      stroke={HISTORY_COLORS[i % HISTORY_COLORS.length]}
                     dot={false}
                     strokeWidth={2}
                     connectNulls={false}
                      isAnimationActive={false}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="empty">{t.noQuotaHistory}</p>
          )}
        </div>
      </section>
    </>
  );
}
