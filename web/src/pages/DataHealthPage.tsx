import { useEffect, useMemo, useState } from "react";
import { fetchSources, type SourceItem } from "../api";
import { type Messages } from "../i18n";
import { tpl } from "../i18n";

export default function DataHealthPage({ t }: { t: Messages }) {
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetchSources()
      .then(setSources)
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, []);

  const issueCount = useMemo(
    () => sources.filter((s) => !["ok", "available"].includes(String(s.state).toLowerCase())).length,
    [sources],
  );

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navDataHealth}</h3>
        <span>{sources.length} {t.sources}</span>
      </div>
      <div className="panel-body">
        <p className="empty">
          {issueCount === 0
            ? tpl(t.allCollecting, { count: sources.length })
            : tpl(t.needAttention, {
                issue: issueCount,
                total: sources.length,
              })}
        </p>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">{t.providerCol}</th>
              <th scope="col">{t.sourceKind}</th>
              <th scope="col">{t.stateCol}</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((item, index) => (
              <tr key={`${item.providerId}-${index}`}>
                <th scope="row">{item.providerId ?? "n/a"}</th>
                <td className="mono">{item.sourceId ?? "n/a"}</td>
                <td>
                  <span
                    className={`pill pill-${
                      item.state === "ok" ? "ok" : item.state === "error" ? "bad" : "warn"
                    }`}
                  >
                    {item.state ?? "unknown"}
                  </span>
                </td>
              </tr>
            ))}
            {sources.length === 0 && !error ? (
              <tr>
                <td colSpan={3} className="empty">
                  {t.comingSoon}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {error ? <p className="empty">{error}</p> : null}
    </section>
  );
}
