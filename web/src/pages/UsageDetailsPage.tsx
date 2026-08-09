import { useEffect, useMemo, useState } from "react";
import { fetchActivity, type ActivityRow } from "../api";
import PeriodSelector, {
  periodDays,
  rangeFor,
  type Period,
} from "../components/PeriodSelector";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";

interface BreakdownRow {
  provider: string;
  model: string;
  source: string;
  input: number;
  output: number;
  total: number;
}

function formatCompact(n: number): string {
  try {
    return new Intl.NumberFormat(navigator.language, {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(n);
  } catch {
    return Math.round(n).toLocaleString();
  }
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

  const byDay = useMemo(() => {
    const map: Record<string, number> = {};
    for (const row of rows) {
      const day = row.day ?? "unknown";
      map[day] = (map[day] ?? 0) + (row.totalTokens ?? 0);
    }
    return Object.entries(map)
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
      .map(([day, tokens]) => ({ day, tokens }));
  }, [rows]);

  const max = Math.max(1, ...byDay.map((d) => d.tokens));

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
              <p className="metric-value">{formatCompact(totals.tokens)}</p>
              <p className="metric-label">{t.totalTokens}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{formatCompact(totals.input)}</p>
              <p className="metric-label">{t.inputTokens}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{formatCompact(totals.output)}</p>
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
                {totals.peakValue ? ` · ${formatCompact(totals.peakValue)}` : ""}
              </span>
            </div>
            <div className="panel-body">
              {byDay.length > 0 ? (
                <div className="trend-bars" role="img" aria-label={t.dailyActivity}>
                  {byDay.map((d) => (
                    <div
                      className="trend-bar"
                      key={d.day}
                      data-value={`${d.day}: ${d.tokens.toLocaleString()}`}
                      tabIndex={0}
                      aria-label={`${d.day}: ${d.tokens.toLocaleString()} tokens`}
                      style={{ height: `${(d.tokens / max) * 100}%` }}
                    />
                  ))}
                </div>
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
