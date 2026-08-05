import { useEffect, useMemo, useState } from "react";
import { fetchActivity, type ActivityRow } from "../api";
import PeriodSelector, {
  periodDays,
  rangeFor,
  type Period,
} from "../components/PeriodSelector";
import { type Messages } from "../i18n";

export default function UsageDetailsPage({ t }: { t: Messages }) {
  const [period, setPeriod] = useState<Period>("week");
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const days = periodDays(period);

  useEffect(() => {
    const { from, to } = rangeFor(days);
    void fetchActivity(from, to)
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, [days]);

  const totals = useMemo(() => {
    let tokens = 0;
    let input = 0;
    let output = 0;
    for (const row of rows) {
      tokens += row.totalTokens ?? 0;
      input += row.inputTokens ?? 0;
      output += row.outputTokens ?? 0;
    }
    return { tokens, input, output };
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

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} t={t} />

      <div className="metrics">
        <div className="metric">
          <p className="metric-label">{t.todayTokens}</p>
          <p className="metric-value">{totals.tokens.toLocaleString()}</p>
        </div>
        <div className="metric">
          <p className="metric-label">{t.inputTokens}</p>
          <p className="metric-value">{totals.input.toLocaleString()}</p>
        </div>
        <div className="metric">
          <p className="metric-label">{t.outputTokens}</p>
          <p className="metric-value">{totals.output.toLocaleString()}</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.modelTrend}</h3>
          <span>{period}</span>
        </div>
        <div className="panel-body">
          <div className="trend-bars" role="img" aria-label={t.modelTrend}>
            {byDay.map((d) => (
              <div
                className="trend-bar"
                key={d.day}
                title={`${d.day}: ${d.tokens.toLocaleString()}`}
                style={{ height: `${(d.tokens / max) * 100}%` }}
              />
            ))}
          </div>
          {byDay.length === 0 ? <p className="empty">{t.noModelTrend}</p> : null}
        </div>
        {byDay.length > 0 ? (
          <div className="panel-body" style={{ paddingTop: 0 }}>
            <p className="dim" style={{ margin: 0, fontSize: "0.78rem" }}>
              {byDay[0].day} - {byDay[byDay.length - 1].day} ·{" "}
              {t.tokensCol}: {totals.tokens.toLocaleString()} · peak{" "}
              {byDay.reduce((best, day) =>
                day.tokens > best.tokens ? day : best,
              ).day}
            </p>
          </div>
        ) : null}
      </section>
      {error ? <p className="empty">{error}</p> : null}
    </>
  );
}
