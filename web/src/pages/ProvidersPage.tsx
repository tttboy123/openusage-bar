import { useEffect, useState } from "react";
import { Plus } from "@phosphor-icons/react";
import { fetchProviders, fetchQuickConnect, type ProviderItem, type QuickConnectItem } from "../api";
import AddProviderDialog from "../components/AddProviderDialog";
import { type Messages } from "../i18n";

export default function ProvidersPage({ t }: { t: Messages }) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
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
    <>
      <div className="toolbar" style={{ marginBottom: 14 }}>
        <button
          type="button"
          className="primary-btn"
          onClick={() => setDialogOpen(true)}
        >
          <Plus size={16} />
          {t.addConnection}
        </button>
        <span className="dim">{providers.length} {t.instances}</span>
      </div>
      <AddProviderDialog
        open={dialogOpen}
        presets={quick}
        onClose={() => setDialogOpen(false)}
        t={t}
      />
      <section className="panel">
        <div className="panel-head">
          <h3>{t.navProviders}</h3>
          <span>{providers.length} {t.instances}</span>
        </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">{t.providerCol}</th>
              <th scope="col">{t.nameCol}</th>
              <th scope="col">{t.sourceKind}</th>
              <th scope="col">{t.consoleCol}</th>
              <th scope="col">{t.apiKey}</th>
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
    </>
  );
}
