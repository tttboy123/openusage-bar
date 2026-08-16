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
import {
  fetchActivity,
  fetchProviders,
  type ActivityRow,
  type ProviderItem,
} from "../api";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";

const CATALOG_LOCAL_TOOL_FAMILIES = new Set([
  "amp", "cc_switch", "codebuff", "crush", "droid", "goose", "hermes",
  "kilo_code", "kimi_cli", "mux", "ollama", "omniroute", "openclaw", "pi",
  "qwen_cli", "roocode", "zed",
]);

function rangeDays() {
  const from = new Date();
  from.setDate(from.getDate() - 30);
  return {
    from: from.toISOString().slice(0, 10),
    to: new Date().toISOString().slice(0, 10),
  };
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

function makeSummaries(
  rows: ActivityRow[],
  providers: ProviderItem[],
): ToolSummary[] {
  // Local tools come from the provider catalog's local_tool category, not a
  // hand-maintained list. Show every catalog local-tool family that has been
  // observed, plus any activity rows that map to a local_tool family.
  const observed = new Set(
    providers
      .filter((p) => p.category === "local_tool")
      .map((p) => p.providerId ?? p.familyId ?? ""),
  );
  const byProvider = new Map<string, ActivityRow[]>();
  for (const row of rows) {
    const id = row.providerId ?? "unknown";
    if (!CATALOG_LOCAL_TOOL_FAMILIES.has(id) && !observed.has(id)) continue;
    const list = byProvider.get(id) ?? [];
    list.push(row);
    byProvider.set(id, list);
  }
  const displayName = new Map(
    providers.map((p) => [
      p.providerId ?? p.familyId ?? "",
      p.displayName ?? p.providerId ?? p.familyId ?? "",
    ]),
  );
  const ids = new Set([...byProvider.keys(), ...observed]);
  return Array.from(ids)
    .map((providerId) => {
      const list = byProvider.get(providerId) ?? [];
      const days = new Set(list.map((r) => r.day).filter(Boolean));
      const models = Array.from(
        new Set(list.map((r) => r.modelId).filter((m): m is string => Boolean(m))),
      );
      const lastDay = list.map((r) => r.day).filter(Boolean).sort().pop() ?? null;
      const tokens = list.reduce((sum, r) => sum + (r.totalTokens ?? 0), 0);
      return {
        providerId,
        displayName: displayName.get(providerId) ?? providerId,
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
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const { from, to } = rangeDays();
    setLoading(true);
    void Promise.all([fetchActivity(from, to), fetchProviders()])
      .then(([activity, providerList]) => {
        setRows(activity.rows);
        setProviders(providerList);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"))
      .finally(() => setLoading(false));
  }, []);

  const localRows = useMemo(
    () =>
      rows.filter((r) => {
        const id = r.providerId ?? "";
        return (
          CATALOG_LOCAL_TOOL_FAMILIES.has(id) ||
          providers.some((p) => p.category === "local_tool" && (p.providerId ?? p.familyId) === id)
        );
      }),
    [rows, providers],
  );

  const totals = useMemo(() => {
    const providerTotals = new Map<string, number>();
    const modelTotals = new Map<string, number>();
    for (const row of localRows) {
      providerTotals.set(
        row.providerId ?? "unknown",
        (providerTotals.get(row.providerId ?? "unknown") ?? 0) + (row.totalTokens ?? 0),
      );
      const key = `${row.providerId ?? "unknown"}/${row.modelId ?? "unknown"}`;
      modelTotals.set(key, (modelTotals.get(key) ?? 0) + (row.totalTokens ?? 0));
    }
    return {
      providers: [...providerTotals.entries()]
        .map(([provider, tokens]) => ({ provider, tokens }))
        .sort((a, b) => b.tokens - a.tokens),
      models: [...modelTotals.entries()]
        .map(([key, tokens]) => ({ key, tokens }))
        .sort((a, b) => b.tokens - a.tokens),
    };
  }, [localRows]);

  const summaries = useMemo(
    () => makeSummaries(localRows, providers),
    [localRows, providers],
  );

  const allModels = new Set(localRows.map((r) => r.modelId).filter(Boolean));
  const activeDays = new Set(localRows.map((r) => r.day).filter(Boolean)).size;
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
          <p className="metric-value">{allModels.size}</p>
          <p className="metric-label">{t.models}</p>
        </div>
        <div className="metric">
          <p className="metric-value">{activeDays}</p>
          <p className="metric-label">{t.activeDays}</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.localTool}</h3>
          <span>{summaries.length} {t.sources}</span>
        </div>
        <div className="panel-body">
          {summaries.length > 0 ? (
            <ul className="tool-list">
              {summaries.map((tool) => (
                <li className="tool-row" key={tool.providerId}>
                  <span className="tool-avatar">
                    <Wrench size={16} />
                  </span>
                  <span className="tool-main">
                    <span className="tool-name">{tool.displayName}</span>
                    <span className="tool-meta">
                      {tool.activeDays} {t.activeDays} · {tool.knownModels.length}{" "}
                      {t.models}
                    </span>
                  </span>
                  <span className="tool-tokens mono">
                    {tool.observedTokens.toLocaleString()}
                  </span>
                  <span className={`tool-state ${tool.state}`}>
                    {tool.state === "ok" ? (
                      <>
                        <CheckCircle size={13} /> {t.statusOk}
                      </>
                    ) : tool.state === "stale" ? (
                      <>
                        <Clock size={13} /> {t.statusWarn}
                      </>
                    ) : (
                      <>{t.statusNeutral}</>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">{t.noRows}</p>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.dailyActivity}</h3>
        </div>
        <div className="panel-body">
          {totals.providers.length > 0 ? (
            <div style={{ width: "100%", height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={totals.providers}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--hairline)" />
                  <XAxis
                    dataKey="provider"
                    tick={{ fontSize: 11, fill: "var(--text-faint)" }}
                    stroke="var(--hairline)"
                  />
                  <YAxis
                    tickFormatter={(v: number) =>
                      v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v)
                    }
                    tick={{ fontSize: 11, fill: "var(--text-faint)" }}
                    stroke="var(--hairline)"
                    width={60}
                  />
                  <Tooltip
                    cursor={{ fill: "color-mix(in srgb, var(--accent) 8%, transparent)" }}
                  />
                  <Bar dataKey="tokens" fill="var(--accent)" maxBarSize={48} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="empty">{t.noRows}</p>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.modelDetail}</h3>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">{t.localTool}</th>
                <th scope="col">{t.modelCol}</th>
                <th scope="col" className="num">{t.tokensCol}</th>
              </tr>
            </thead>
            <tbody>
              {totals.models.map((row) => {
                const [provider, model] = row.key.split("/");
                return (
                  <tr key={row.key}>
                    <th scope="row">{provider}</th>
                    <td className="mono">{model}</td>
                    <td className="mono num">{row.tokens.toLocaleString()}</td>
                  </tr>
                );
              })}
              {totals.models.length === 0 ? (
                <tr>
                  <td colSpan={3} className="empty">
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
  );
}
