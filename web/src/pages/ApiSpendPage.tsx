import { useEffect, useMemo, useState } from "react";
import { fetchActivity, fetchCosts, type ActivityRow, type CostRow } from "../api";
import PeriodSelector, {
  periodDays,
  rangeFor,
  type Period,
} from "../components/PeriodSelector";
import { type Messages } from "../i18n";

export default function ApiSpendPage({ t }: { t: Messages }) {
  const [period, setPeriod] = useState<Period>("week");
  const [costs, setCosts] = useState<CostRow[]>([]);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const { from, to } = rangeFor(periodDays(period));
    Promise.all([fetchCosts(from, to), fetchActivity(from, to)])
      .then(([c, a]) => {
        setCosts(c);
        setActivity(a);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, [period]);

  const totals = useMemo(() => {
    const byCurrency: Record<string, number> = {};
    for (const row of costs) {
      const amount = Number(row.amount ?? 0);
      const currency = row.currency ?? "USD";
      byCurrency[currency] = (byCurrency[currency] ?? 0) + amount;
    }
    return Object.entries(byCurrency).map(([currency, amount]) => ({
      currency,
      amount,
    }));
  }, [costs]);

  const tokenByProvider = useMemo(() => {
    const map: Record<string, number> = {};
    for (const row of activity) {
      const provider = row.providerId ?? "unknown";
      map[provider] = (map[provider] ?? 0) + (row.totalTokens ?? 0);
    }
    return Object.entries(map)
      .map(([provider, tokens]) => ({ provider, tokens }))
      .sort((a, b) => b.tokens - a.tokens);
  }, [activity]);

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} />
      <section className="panel">
        <div className="panel-head">
          <h3>{t.navApiSpend}</h3>
          <span>{t.last7Days}</span>
        </div>
        <div className="panel-body">
          {totals.map((total) => (
            <p key={total.currency} className="mono" style={{ fontSize: "1.2rem" }}>
              {total.currency} {total.amount.toFixed(4)}
            </p>
          ))}
          {totals.length === 0 ? <p className="empty">{t.noSpend}</p> : null}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>{t.tokenUsage}</h3>
          <span>{t.last7Days}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">{t.providerCol}</th>
                <th scope="col">{t.tokensCol}</th>
              </tr>
            </thead>
            <tbody>
              {tokenByProvider.map((row) => (
                <tr key={row.provider}>
                  <th scope="row">{row.provider}</th>
                  <td className="mono">{row.tokens.toLocaleString()}</td>
                </tr>
              ))}
              {tokenByProvider.length === 0 ? (
                <tr>
                  <td colSpan={2} className="empty">
                    {t.noSpend}
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
