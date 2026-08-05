import { useEffect, useMemo, useState } from "react";
import {
  ChartLineUp,
  CurrencyCircleDollar,
  HardDrives,
  CalendarBlank,
} from "@phosphor-icons/react";
import { fetchSnapshot, type Snapshot } from "../api";
import { useAnimatedNumber } from "../hooks/useAnimatedNumber";
import { type Messages } from "../i18n";

function Kpi({
  icon,
  label,
  value,
  meta,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  meta?: string;
}) {
  return (
    <div className="metric">
      <p className="metric-label">{label}</p>
      <p className="metric-value">{value}</p>
      {meta ? <p className="metric-meta">{meta}</p> : null}
      <span style={{ display: "none" }}>{icon}</span>
    </div>
  );
}

export default function ActivityPage({ t }: { t: Messages }) {
  const [snapshot, setSnapshot] = useState<Snapshot>({});
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setSnapshot(await fetchSnapshot());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const summary = snapshot.summary ?? {};
  const tokens = useAnimatedNumber(summary.todayTokens);
  const quotas = snapshot.quotaHub ?? [];
  const providers = snapshot.providers ?? [];

  const tokenValue =
    summary.todayTokens === undefined
      ? "unknown"
      : Math.round(tokens).toLocaleString();
  const tokenMeta =
    summary.modelCount !== undefined
      ? `${summary.modelCount} ${t.providers} · ${summary.coveredDayCount ?? 0} days`
      : undefined;

  const trendBars = useMemo(
    () => [38, 52, 44, 61, 48, 70, 82, 64, 76, 90, 58, 85],
    [],
  );

  return (
    <>
      <div className="metrics">
        <Kpi
          icon={<ChartLineUp />}
          label={t.todayTokens}
          value={tokenValue}
          meta={tokenMeta}
        />
        <Kpi
          icon={<HardDrives />}
          label={t.providers}
          value={String(providers.length)}
        />
        <Kpi
          icon={<CurrencyCircleDollar />}
          label={t.balance}
          value={String(quotas.length)}
          meta={quotas.map((q) => q.currency).filter(Boolean).join(", ")}
        />
        <Kpi
          icon={<CalendarBlank />}
          label={t.ledgerDate}
          value={new Date().toISOString().slice(0, 10)}
        />
      </div>

      {error ? <p className="empty">{error}</p> : null}

      <h2 className="section-title">
        {t.quotaHub} <span className="dim">· free quota aggregation</span>
      </h2>
      <div className="quota-grid">
        {quotas.map((item, index) => (
          <article className="quota" key={index}>
            <div className="quota-bar" aria-hidden="true" />
            <p className="quota-currency">{item.currency ?? "n/a"}</p>
            <p className="quota-amount">{item.totalAvailable ?? "n/a"}</p>
            <p className="quota-meta">{item.providerCount ?? 0} {t.providers}</p>
            {item.provenance && item.provenance.length > 0 ? (
              <ul className="provenance">
                {item.provenance.map((part, i) => (
                  <li className="mono" key={i}>
                    {part.join(":")}
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        ))}
        {quotas.length === 0 ? <p className="empty">{t.noSpend}</p> : null}
      </div>

      <section className="panel" aria-label={t.modelTrend}>
        <div className="panel-head">
          <h3>{t.modelTrend}</h3>
          <span>30 days</span>
        </div>
        <div className="panel-body">
          <div className="trend-bars" role="img" aria-label={t.modelTrend}>
            {trendBars.map((height, index) => (
              <div
                className="trend-bar"
                key={index}
                style={{ height: `${height}%` }}
              />
            ))}
          </div>
          <p className="empty">
            <ChartLineUp size={16} />
            {t.noModelTrend}
          </p>
        </div>
      </section>

      <section className="panel" aria-label={t.modelSpend}>
        <div className="panel-head">
          <h3>{t.modelSpend}</h3>
          <span>last 7 days</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Provider</th>
                <th scope="col">Name</th>
                <th scope="col">Source Kind</th>
                <th scope="col">Console</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((item) => (
                <tr key={item.providerId}>
                  <th scope="row">{item.providerId ?? "n/a"}</th>
                  <td>{item.displayName ?? "n/a"}</td>
                  <td className="mono">{item.sourceKind ?? "n/a"}</td>
                  <td>
                    <a
                      className="btn-link"
                      href={`#`}
                      onClick={(e) => e.preventDefault()}
                    >
                      {t.openConsole}
                    </a>
                  </td>
                </tr>
              ))}
              {providers.length === 0 ? (
                <tr>
                  <td colSpan={4} className="empty">
                    {t.noSpend}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
