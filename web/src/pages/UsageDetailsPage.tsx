import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchActivity, type ActivityRow } from "../api";
import PeriodSelector, {
  periodDays,
  rangeFor,
  type Period,
} from "../components/PeriodSelector";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";
import { formatTokenCompact } from "../tokenFormat";

interface BreakdownRow {
  provider: string;
  model: string;
  source: string;
  input: number;
  output: number;
  total: number;
}

const MODEL_PALETTE = [
  "#4D6BFE",
  "#FF6B6B",
  "#10A37F",
  "#D97757",
  "#4285F4",
  "#8E75B7",
  "#0EA5E9",
  "#F59E0B",
  "#7C3AED",
  "#06B6D4",
  "#84CC16",
  "#64748B",
];
const MAX_CHART_MODELS = 10;
const OTHER_KEY = "__other__";

type TooltipItem = {
  name?: unknown;
  value?: unknown;
  color?: unknown;
  dataKey?: unknown;
};

function dailyTooltipContent({
  active,
  payload,
  label,
  t,
  modelNames,
}: {
  active?: boolean;
  payload?: ReadonlyArray<TooltipItem>;
  label?: string | number;
  t: Messages;
  modelNames: Map<string, string>;
}) {
  if (!active || !payload || !payload.length || !label) return null;
  const items = payload
    .map((p) => {
      const raw = Array.isArray(p.value) ? p.value[0] : p.value;
      const value = Number(raw) || 0;
      const key = String(p.dataKey ?? p.name ?? "");
      const name =
        key === "total"
          ? t.totalTokens
          : key === OTHER_KEY
            ? t.otherModels
            : (modelNames.get(key) ?? key);
      return {
        key,
        name,
        value,
        color: String(p.color ?? "var(--text-dim)"),
      };
    })
    // The chart draws a "total" Line on top of the stacked model bars; the
    // tooltip payload therefore also contains that total series. Exclude it
    // so the sum is the models only, otherwise "总 Token" would be added to
    // the model totals and double-counted.
    .filter((p) => p.value > 0 && p.key !== "total");
  const total = items.reduce((sum, p) => sum + p.value, 0);
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

export default function UsageDetailsPage({ t }: { t: Messages }) {
  const [period, setPeriod] = useState<Period>("week");
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const days = periodDays(period);

  useEffect(() => {
    const controller = new AbortController();
    const { from, to } = rangeFor(days);
    setLoading(true);
    void fetchActivity(from, to, undefined, undefined, controller.signal)
      .then(({ rows }) => setRows(rows))
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e instanceof Error ? e.message : "failed");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [days]);

  // Keep the metrics/chart live while the page is open: the collector imports
  // new data every few minutes, so a page left open must not show frozen totals.
  useEffect(() => {
    const timer = setInterval(() => {
      const { from: liveFrom, to: liveTo } = rangeFor(days);
      void fetchActivity(liveFrom, liveTo, undefined, undefined)
        .then(({ rows }) => setRows(rows))
        .catch(() => {
          // Keep the previous rows; the next tick retries.
        });
    }, 60_000);
    return () => clearInterval(timer);
  }, [days]);

  const totals = useMemo(() => {
    let tokens = 0;
    let input = 0;
    let output = 0;
    const byDay: Record<string, number> = {};
    for (const row of rows) {
      tokens += row.totalTokens ?? 0;
      input += row.inputTokens ?? 0;
      output += row.outputTokens ?? 0;
      const day = row.day ?? "unknown";
      byDay[day] = (byDay[day] ?? 0) + (row.totalTokens ?? 0);
    }
    const activeDays = Object.keys(byDay).filter((d) => byDay[d] > 0).length;
    let peakDay = "—";
    let peakValue = 0;
    for (const [day, value] of Object.entries(byDay)) {
      if (value > peakValue) {
        peakValue = value;
        peakDay = day;
      }
    }
    return { tokens, input, output, activeDays, peakDay, peakValue };
  }, [rows]);

  const chart = useMemo(() => {
    const modelTotals = new Map<string, number>();
    const byDayByModel = new Map<string, Map<string, number>>();
    for (const row of rows) {
      const model = row.modelId ?? "unknown";
      const day = row.day ?? "unknown";
      modelTotals.set(model, (modelTotals.get(model) ?? 0) + (row.totalTokens ?? 0));
      const dayMap = byDayByModel.get(day) ?? new Map<string, number>();
      dayMap.set(model, (dayMap.get(model) ?? 0) + (row.totalTokens ?? 0));
      byDayByModel.set(day, dayMap);
    }
    const topModels = [...modelTotals.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, MAX_CHART_MODELS)
      .map(([m]) => m);

    const daysList = [...byDayByModel.keys()].sort();
    const points: Array<Record<string, number | string>> = [];
    const modelKeys = new Map<string, string>();
    for (const day of daysList) {
      const dayMap = byDayByModel.get(day) ?? new Map();
      const point: Record<string, number | string> = { day };
      let total = 0;
      let other = 0;
      topModels.forEach((model, index) => {
        const key = `m${index}`;
        modelKeys.set(key, model);
        const value = dayMap.get(model) ?? 0;
        point[key] = value;
        total += value;
      });
      for (const [model, value] of dayMap) {
        if (!topModels.includes(model)) {
          other += value;
          total += value;
        }
      }
      if (other > 0) point[OTHER_KEY] = other;
      point.total = total;
      points.push(point);
    }
    return { points, modelKeys, otherLabel: OTHER_KEY };
  }, [rows]);

  const breakdown = useMemo(() => {
    const map = new Map<string, BreakdownRow>();
    for (const row of rows) {
      const provider = row.providerId ?? "unknown";
      const model = row.modelId ?? "unknown";
      const source = row.sourceId ?? "unknown";
      const key = `${provider}\u0000${model}\u0000${source}`;
      const entry = map.get(key) ?? {
        provider,
        model,
        source,
        input: 0,
        output: 0,
        total: 0,
      };
      entry.input += row.inputTokens ?? 0;
      entry.output += row.outputTokens ?? 0;
      entry.total += row.totalTokens ?? 0;
      map.set(key, entry);
    }
    return [...map.values()].sort((a, b) => b.total - a.total);
  }, [rows]);

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} t={t} />

      {loading ? (
        <Skeleton lines={5} />
      ) : (
        <>
          <div className="metrics">
            <div className="metric">
              <p className="metric-value">{formatTokenCompact(totals.tokens)}</p>
              <p className="metric-label">{t.totalTokens}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{formatTokenCompact(totals.input)}</p>
              <p className="metric-label">{t.inputTokens}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{formatTokenCompact(totals.output)}</p>
              <p className="metric-label">{t.outputTokens}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{totals.activeDays}</p>
              <p className="metric-label">{t.activeDays}</p>
            </div>
          </div>

          <section className="panel">
            <div className="panel-head">
              <h3>{t.dailyActivity}</h3>
              <span>
                {t.peakDay}: {totals.peakDay}
                {totals.peakValue ? ` · ${formatTokenCompact(totals.peakValue)}` : ""}
              </span>
            </div>
            <div className="panel-body">
              {chart.points.length > 0 ? (
                <>
                  <div className="trend-chart" role="img" aria-label={t.dailyActivity}>
                    <ResponsiveContainer width="100%" height={260}>
                      <ComposedChart
                        data={chart.points}
                        margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--hairline)" />
                        <XAxis
                          dataKey="day"
                          tickFormatter={(v: string) => v.slice(5)}
                          tick={{ fontSize: 11, fill: "var(--text-faint)" }}
                          stroke="var(--hairline)"
                          interval="preserveStartEnd"
                          minTickGap={16}
                        />
                        <YAxis
                          tickFormatter={(v: number) => formatTokenCompact(v)}
                          tick={{ fontSize: 11, fill: "var(--text-faint)" }}
                          stroke="var(--hairline)"
                          width={64}
                        />
                        <Tooltip
                          cursor={{ fill: "color-mix(in srgb, var(--accent) 8%, transparent)" }}
                          content={(props) =>
                            dailyTooltipContent({
                              ...(props as unknown as {
                                active?: boolean;
                                payload?: ReadonlyArray<TooltipItem>;
                                label?: string | number;
                              }),
                              t,
                              modelNames: chart.modelKeys,
                            })
                          }
                        />
                        {Array.from(chart.modelKeys.entries()).map(([key, model], i) => (
                          <Bar
                            key={key}
                            dataKey={key}
                            name={model}
                            stackId="tokens"
                            fill={MODEL_PALETTE[i % MODEL_PALETTE.length]}
                            maxBarSize={42}
                          />
                        ))}
                        {chart.points.some((p) => (p[OTHER_KEY] as number) > 0) ? (
                          <Bar
                            dataKey={OTHER_KEY}
                            name={t.otherModels}
                            stackId="tokens"
                            fill="var(--text-faint)"
                            maxBarSize={42}
                          />
                        ) : null}
                        <Line
                          dataKey="total"
                          name={t.totalTokens}
                          stroke="var(--text)"
                          strokeWidth={2}
                          dot={false}
                          activeDot={{ r: 3 }}
                        />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="trend-legend">
                    {Array.from(chart.modelKeys.entries()).map(([key, model], i) => (
                      <span className="trend-legend-item" key={key}>
                        <span
                          className="trend-legend-dot"
                          style={{ background: MODEL_PALETTE[i % MODEL_PALETTE.length] }}
                        />
                        {model}
                      </span>
                    ))}
                    {chart.points.some((p) => (p[OTHER_KEY] as number) > 0) ? (
                      <span className="trend-legend-item">
                        <span
                          className="trend-legend-dot"
                          style={{ background: "var(--text-faint)" }}
                        />
                        {t.otherModels}
                      </span>
                    ) : null}
                    <span className="trend-legend-item trend-legend-total">
                      <span className="trend-legend-line" />
                      {t.totalTokens}
                    </span>
                  </div>
                </>
              ) : (
                <p className="empty">{t.noRows}</p>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h3>{t.breakdown}</h3>
              <span>{rows.length} {t.sources}</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.providerCol}</th>
                    <th scope="col">{t.modelCol}</th>
                    <th scope="col">{t.sourceCol}</th>
                    <th scope="col" className="num">{t.inputTokens}</th>
                    <th scope="col" className="num">{t.outputTokens}</th>
                    <th scope="col" className="num">{t.tokensCol}</th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.map((row, i) => (
                    <tr key={`${row.provider}/${row.model}/${row.source}/${i}`}>
                      <th scope="row">{row.provider}</th>
                      <td className="mono">{row.model}</td>
                      <td className="mono dim">{row.source}</td>
                      <td className="mono num">{row.input.toLocaleString()}</td>
                      <td className="mono num">{row.output.toLocaleString()}</td>
                      <td className="mono num">{row.total.toLocaleString()}</td>
                    </tr>
                  ))}
                  {breakdown.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="empty">
                        {t.noRows}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </section>

          {error ? <p className="empty">{error}</p> : null}
        </>
      )}
    </>
  );
}
