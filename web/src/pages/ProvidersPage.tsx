import { useEffect, useState } from "react";
import { fetchProviders, fetchQuickConnect, type ProviderItem, type QuickConnectItem } from "../api";
import { type Messages } from "../i18n";

export default function ProvidersPage({ t }: { t: Messages }) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([fetchProviders(), fetchQuickConnect()])
      .then(([p, q]) => {
        setProviders(p);
        setQuick(q);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, []);

  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navProviders}</h3>
        <span>{providers.length} instances</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Provider</th>
              <th scope="col">Name</th>
              <th scope="col">Source Kind</th>
              <th scope="col">Console</th>
              <th scope="col">API Key</th>
            </tr>
          </thead>
          <tbody>
            {providers.map((item) => {
              const quick = quickByFamily.get(item.familyId ?? item.providerId ?? "");
              return (
                <tr key={item.providerId}>
                  <th scope="row">{item.providerId ?? "n/a"}</th>
                  <td>{item.displayName ?? "n/a"}</td>
                  <td className="mono">{item.sourceKind ?? "n/a"}</td>
                  <td>
                    {quick?.consoleUrl ? (
                      <a
                        className="btn-link"
                        href={quick.consoleUrl}
                        target="_blank"
                        rel="noopener"
                      >
                        {t.openConsole}
                      </a>
                    ) : (
                      "n/a"
                    )}
                  </td>
                  <td>
                    {quick?.apiKeyUrl ? (
                      <a
                        className="btn-link"
                        href={quick.apiKeyUrl}
                        target="_blank"
                        rel="noopener"
                      >
                        {t.getApiKey}
                      </a>
                    ) : (
                      "n/a"
                    )}
                  </td>
                </tr>
              );
            })}
            {providers.length === 0 && !error ? (
              <tr>
                <td colSpan={5} className="empty">
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
