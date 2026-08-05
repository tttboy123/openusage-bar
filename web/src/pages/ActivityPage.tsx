import { useEffect, useState } from "react";
import {
  ChartLineUp,
  CurrencyCircleDollar,
  HardDrives,
  CalendarBlank,
} from "@phosphor-icons/react";
import {
  fetchActivity,
  fetchQuickConnect,
  fetchSnapshot,
  type QuickConnectItem,
  type Snapshot,
} from "../api";
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
      <p className="metric-label">
        <span className="metric-icon" aria-hidden="true">
          {icon}
        </span>
        {label}
      </p>
      <p className="metric-value">{value}</p>
      {meta ? <p className="metric-meta">{meta}</p> : null}
    </div>
  );
}

export default function ActivityPage({ t }: { t: Messages }) {
  const [snapshot, setSnapshot] = useState<Snapshot>({});
  const [error, setError] = useState<string | null>(null);
  const [trend, setTrend] = useState<{ day: string; tokens: number }[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);

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

  useEffect(() => {
    void fetchQuickConnect().then(setQuick).catch(() => {});
  }, []);

  useEffect(() => {
    const from = new Date();
    from.setDate(from.getDate() - 29);
    void fetchActivity(
      from.toISOString().slice(0, 10),
      new Date().toISOString().slice(0, 10),
    )
      .then((rows) => {
        const map: Record<string, number> = {};
        for (const row of rows) {
          const day = row.day ?? "unknown";
          map[day] = (map[day] ?? 0) + (row.totalTokens ?? 0);
        }
        setTrend(
          Object.entries(map)
            .sort((a, b) => (a[0] < b[0] ? -1 : 1))
            .map(([day, tokens]) => ({ day, tokens })),
        );
      })
      .catch(() => {});
  }, []);

  const summary = snapshot.summary ?? {};
  const tokens = useAnimatedNumber(summary.todayTokens);
  const quotas = snapshot.quotaHub ?? [];
  const providers = snapshot.providers ?? [];
  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));

  const tokenValue =
    summary.todayTokens === undefined
      ? "unknown"
      : Math.round(tokens).toLocaleString();
  const tokenMeta =
    summary.modelCount !== undefined
      ? `${summary.modelCount} ${t.providers} · ${summary.coveredDayCount ?? 0} days`
      : undefined;

  const trendMax = Math.max(1, ...trend.map((entry) => entry.tokens));

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
        {t.quotaHub} <span className="dim">· {t.freeQuotaAggregation}</span>
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
          <span>{t.last30Days}</span>
        </div>
        <div className="panel-body">
          <div className="trend-bars" role="img" aria-label={t.modelTrend}>
            {trend.map((entry) => (
              <div
                className="trend-bar"
                key={entry.day}
                title={`${entry.day}: ${entry.tokens.toLocaleString()}`}
                style={{ height: `${(entry.tokens / trendMax) * 100}%` }}
              />
            ))}
          </div>
          {trend.length === 0 ? (
            <p className="empty">
              <ChartLineUp size={16} />
              {t.noModelTrend}
            </p>
          ) : null}
        </div>
      </section>

      <section className="panel" aria-label={t.modelSpend}>
        <div className="panel-head">
          <h3>{t.modelSpend}</h3>
          <span>{t.last7Days}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">{t.providerCol}</th>
                <th scope="col">{t.nameCol}</th>
                <th scope="col">{t.sourceKind}</th>
                <th scope="col">{t.consoleCol}</th>
              </tr>
            </thead>
            <tbody>
            {providers.map((item) => (
              <tr key={item.providerId}>
                <th scope="row">{item.providerId ?? "n/a"}</th>
                <td>{item.displayName ?? "n/a"}</td>
                <td className="mono">{item.sourceKind ?? "n/a"}</td>
                <td>
                  {(() => {
                    const quick = quickByFamily.get(
                      item.familyId ?? item.providerId ?? "",
                    );
                    return quick?.consoleUrl ? (
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
                    );
                  })()}
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
