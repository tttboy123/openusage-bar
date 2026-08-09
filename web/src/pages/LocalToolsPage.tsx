import { useEffect, useMemo, useState } from "react";
import { Wrench, CheckCircle, Clock } from "@phosphor-icons/react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import { fetchActivity, type ActivityRow } from "../api";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";

const LOCAL_TOOL_FAMILIES = new Set(["hermes", "openclaw", "kiro_cli"]);
const DISPLAY_NAMES: Record<string, string> = {
  hermes: "Hermes",
  openclaw: "OpenClaw",
  kiro_cli: "Kiro CLI",
};

function rangeDays() {
  const from = new Date();
  from.setDate(from.getDate() - 30);
  return { from: from.toISOString().slice(0, 10), to: new Date().toISOString().slice(0, 10) };
}

interface ToolSummary {
  providerId: string;
  displayName: string;
  observedTokens: number;
  activeDays: number;
  knownModels: string[];
  lastActivityDay: string | null;
  state: "ok" | "stale" | "unavailable";
}

function makeSummaries(rows: ActivityRow[]): ToolSummary[] {
  const byProvider = new Map<string, ActivityRow[]>();
  for (const row of rows) {
    const id = row.providerId ?? "unknown";
    if (!LOCAL_TOOL_FAMILIES.has(id)) continue;
    const list = byProvider.get(id) ?? [];
    list.push(row);
    byProvider.set(id, list);
  }
  return Array.from(byProvider.entries())
    .map(([providerId, list]) => {
      const days = new Set(list.map((r) => r.day).filter(Boolean));
      const models = Array.from(new Set(list.map((r) => r.modelId).filter((m): m is string => Boolean(m))));
      const lastDay = list.map((r) => r.day).filter(Boolean).sort().pop() ?? null;
      const tokens = list.reduce((sum, r) => sum + (r.totalTokens ?? 0), 0);
      return {
        providerId,
        displayName: DISPLAY_NAMES[providerId] ?? providerId,
        observedTokens: tokens,
        activeDays: days.size,
        knownModels: models,
        lastActivityDay: lastDay,
        state: lastDay
          ? new Date().getTime() - new Date(`${lastDay}T00:00:00`).getTime() < 3 * 86400000
            ? ("ok" as const)
            : ("stale" as const)
          : ("unavailable" as const),
      };
    })
    .sort((a, b) => b.observedTokens - a.observedTokens);
}

export default function LocalToolsPage({ t }: { t: Messages }) {
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const { from, to } = rangeDays();
    setLoading(true);
    void fetchActivity(from, to)
      .then(({ rows }) => {
        setRows(rows.filter((r) => LOCAL_TOOL_FAMILIES.has(r.providerId ?? "")));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"))
      .finally(() => setLoading(false));
  }, []);

  const totals = useMemo(() => {
    const providerTotals = new Map<string, number>();
    const modelTotals = new Map<string, number>();
    for (const row of rows) {
      providerTotals.set(row.providerId ?? "unknown", (providerTotals.get(row.providerId ?? "unknown") ?? 0) + (row.totalTokens ?? 0));
      const key = `${row.providerId ?? "unknown"}/${row.modelId ?? "unknown"}`;
      modelTotals.set(key, (modelTotals.get(key) ?? 0) + (row.totalTokens ?? 0));
    }
    return {
      providers: [...providerTotals.entries()].map(([provider, tokens]) => ({ provider, tokens })).sort((a, b) => b.tokens - a.tokens),
      models: [...modelTotals.entries()].map(([key, tokens]) => ({ key, tokens })).sort((a, b) => b.tokens - a.tokens),
    };
  }, [rows]);

  const summaries = useMemo(() => makeSummaries(rows), [rows]);

  const allModels = new Set(rows.map((r) => r.modelId).filter(Boolean));
  const activeDays = new Set(rows.map((r) => r.day).filter(Boolean)).size;
  const totalTokens = totals.providers.reduce((sum, p) => sum + p.tokens, 0);

  if (loading) return <Skeleton lines={5} />;

  return (
    <>
      <div className="metrics">
        <div className="metric">
          <p className="metric-value">{summaries.length}</p>
          <p className="metric-label">{t.localTool}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{totalTokens.toLocaleString()}</p>
          <p className="metric-label">{t.totalTokens}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{activeDays}</p>
          <p className="metric-label">{t.activeDays}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{allModels.size}</p>
          <p className="metric-label">{t.knownModels}</p>
        </div>
      </div>

      <section className="panel" aria-label={t.localToolSummary}>
        <div className="panel-head">
          <h3>{t.localToolSummary}</h3>
          <span>{t.last30Days}</span>
        </div>
        <div className="panel-body">
          {totals.providers.length > 0 ? (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={totals.providers}>
                 <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="provider" tickFormatter={(p: string | number) => String(p).slice(0, 12)} />
                  <YAxis />
                  <Tooltip
                    formatter={(value: number | string | readonly (number | string)[] | undefined, _name: number | string | undefined) => [Number(value).toLocaleString(), t.tokensCol]}
                  />
                  <Bar dataKey="tokens" fill="var(--accent)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="empty">{t.noLocalToolActivity}</p>
          )}
        </div>
      </section>

      <section className="panel" aria-label={t.localToolRecent}>
        <div className="panel-head">
          <h3>
            <Wrench size={16} style={{ marginRight: 6, verticalAlign: -2 }} />
            {t.localToolRecent}
          </h3>
          <span>{rows.length}</span>
        </div>
        <div className="panel-body">
          {summaries.length > 0 ? (
            <div className="local-tool-summaries">
              {summaries.map((s) => (
                <div key={s.providerId} className="local-tool-card">
                  <div className="local-tool-card-head">
                    <div className="local-tool-card-title">
                      <span className="local-tool-card-name">{s.displayName}</span>
                      <span className="local-tool-card-subtitle dim">{t.localRuntime}</span>
                    </div>
                    <span className={`status-badge ${s.state === "ok" ? "status-ok" : "status-neutral"}`}>
                      <CheckCircle size={12} weight="fill" style={{ marginRight: 4, verticalAlign: -2 }} />
                      {s.state === "ok" ? t.statusOk : t.statusNeutral}
                    </span>
                  </div>
                  <div className="local-tool-metrics">
                    <div className="local-tool-metric">
                      <span className="local-tool-metric-label">{t.observedTokens}</span>
                      <span className="local-tool-metric-value">{s.observedTokens.toLocaleString()}</span>
                    </div>
                    <div className="local-tool-metric">
                      <span className="local-tool-metric-label">{t.activeDays}</span>
                      <span className="local-tool-metric-value">{s.activeDays}</span>
                    </div>
                    <div className="local-tool-metric">
                      <span className="local-tool-metric-label">{t.knownModels}</span>
                      <span className="local-tool-metric-value">{s.knownModels.length}</span>
                    </div>
                    <div className="local-tool-metric">
                      <span className="local-tool-metric-label">{t.lastActivity}</span>
                      <span className="local-tool-metric-value">{s.lastActivityDay ?? "—"}</span>
                    </div>
                  </div>
                  {s.knownModels.length > 0 && (
                    <div className="local-tool-models">
                      <span className="dim"><Clock size={12} style={{ marginRight: 4, verticalAlign: -2 }} /> {t.collectedAt} {s.lastActivityDay ?? "—"}</span>
                      <span className="local-tool-model-list dim" title={s.knownModels.join(", ")}>
                        {s.knownModels.slice(0, 3).join(", ")}
                        {s.knownModels.length > 3 && ` +${s.knownModels.length - 3}`}
                      </span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="empty">{error ? error : t.noLocalToolActivity}</p>
          )}
        </div>
      </section>
    </>
  );
}
