import { useEffect, useMemo, useState } from "react";
import { fetchActivity, type ActivityRow } from "../api";
import { type Messages } from "../i18n";

const PERIODS = [
  { key: "day", days: 1 },
  { key: "week", days: 7 },
  { key: "month", days: 30 },
  { key: "year", days: 365 },
] as const;

function range(days: number) {
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - days + 1);
  return {
    from: from.toISOString().slice(0, 10),
    to: to.toISOString().slice(0, 10),
  };
}

export default function UsageDetailsPage({ t }: { t: Messages }) {
  const [period, setPeriod] = useState<(typeof PERIODS)[number]["key"]>("week");
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const days = PERIODS.find((p) => p.key === period)!.days;

  useEffect(() => {
    const { from, to } = range(days);
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
      <div className="toolbar" style={{ marginBottom: 16 }}>
        {PERIODS.map((p) => (
          <button
            type="button"
            key={p.key}
            className={`icon-btn${period === p.key ? "" : ""}`}
            style={{
              background: period === p.key ? "var(--accent-soft)" : undefined,
              color: period === p.key ? "var(--accent)" : undefined,
            }}
            onClick={() => setPeriod(p.key)}
          >
            {p.key}
          </button>
        ))}
      </div>

      <div className="metrics">
        <div className="metric">
          <p className="metric-label">{t.todayTokens}</p>
          <p className="metric-value">{totals.tokens.toLocaleString()}</p>
        </div>
        <div className="metric">
          <p className="metric-label">Input</p>
          <p className="metric-value">{totals.input.toLocaleString()}</p>
        </div>
        <div className="metric">
          <p className="metric-label">Output</p>
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
      </section>
      {error ? <p className="empty">{error}</p> : null}
    </>
  );
}
