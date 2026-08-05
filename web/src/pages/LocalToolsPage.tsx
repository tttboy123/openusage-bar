import { useEffect, useMemo, useState } from "react";
import { fetchActivity, type ActivityRow } from "../api";
import { type Messages } from "../i18n";

const LOCAL_TOOL_FAMILIES = new Set(["hermes", "openclaw", "kiro_cli"]);

export default function LocalToolsPage({ t }: { t: Messages }) {
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const from = new Date();
    from.setDate(from.getDate() - 30);
    void fetchActivity(from.toISOString().slice(0, 10), new Date().toISOString().slice(0, 10))
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, []);

  const localRows = useMemo(
    () => rows.filter((r) => LOCAL_TOOL_FAMILIES.has(r.providerId ?? "")),
    [rows],
  );

  const totals = useMemo(() => {
    const map: Record<string, number> = {};
    for (const row of localRows) {
      const provider = row.providerId ?? "unknown";
      map[provider] = (map[provider] ?? 0) + (row.totalTokens ?? 0);
    }
    return Object.entries(map).map(([provider, tokens]) => ({ provider, tokens }));
  }, [localRows]);

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navLocalTools}</h3>
        <span>last 30 days</span>
      </div>
      <div className="panel-body">
        {totals.length === 0 ? (
          <p className="empty">
            No local tool activity yet. Hermes and OpenClaw appear here after they produce
            Token usage on this device.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Tool</th>
                  <th scope="col">Tokens</th>
                </tr>
              </thead>
              <tbody>
                {totals.map((row) => (
                  <tr key={row.provider}>
                    <th scope="row">{row.provider}</th>
                    <td className="mono">{row.tokens.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {error ? <p className="empty">{error}</p> : null}
    </section>
  );
}
