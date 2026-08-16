import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchActivity,
  fetchCosts,
  fetchProviders,
  type ActivityRow,
  type CostRow,
  type ProviderItem,
} from "../api";
import PeriodSelector, {
  periodDays,
  rangeFor,
  type Period,
} from "../components/PeriodSelector";
import Skeleton from "../components/Skeleton";
import { type Messages } from "../i18n";

const PERIOD_LABEL: Record<Period, keyof Messages> = {
  day: "periodDay",
  week: "periodWeek",
  month: "periodMonth",
  year: "periodYear",
};

interface ModelRow {
  provider: string;
  model: string;
  sources: string[];
  tokens: number;
  costAmount?: string | null;
  costCurrency?: string | null;
}

function formatAmount(amount: number, currency: string): string {
  return `${currency} ${amount.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export default function ApiSpendPage({ t }: { t: Messages }) {
  const [period, setPeriod] = useState<Period>("week");
  const [costs, setCosts] = useState<CostRow[]>([]);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const { from, to } = rangeFor(periodDays(period));
    setLoading(true);
    Promise.all([fetchCosts(from, to), fetchActivity(from, to), fetchProviders()])
      .then(([c, a, p]) => {
        setCosts(c);
        setActivity(a.rows);
        setProviders(p);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed"))
      .finally(() => setLoading(false));
  }, [period]);

  const apiProviders = useMemo(
    () =>
      new Set(
        providers
          .filter((p) => p.category === "api")
          .map((p) => p.providerId ?? ""),
      ),
    [providers],
  );

  // Real provider-reported API spend comes from per-model activity rows.
  const spendByCurrency = useMemo(() => {
    const byCurrency: Record<string, number> = {};
    for (const row of activity) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const amount = Number(row.costAmount ?? 0);
      if (!(amount > 0)) continue;
      const currency = row.costCurrency ?? "USD";
      byCurrency[currency] = (byCurrency[currency] ?? 0) + amount;
    }
    return Object.entries(byCurrency)
      .map(([currency, amount]) => ({ currency, amount }))
      .filter((entry) => entry.amount > 0)
      .sort((a, b) => b.amount - a.amount);
  }, [activity, apiProviders]);

  // Credit-window ledger rows (e.g. openusage.window_credit_spend) are a
  // separate estimate basis and can be partial; show them apart from real
  // spend instead of blending them in.
  const creditCosts = useMemo(() => {
    const byProvider = new Map<string, CostRow[]>();
    for (const row of costs) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const provider = row.providerId ?? "unknown";
      const list = byProvider.get(provider) ?? [];
      list.push(row);
      byProvider.set(provider, list);
    }
    return [...byProvider.entries()].map(([provider, rows]) => {
      const byCurrency: Record<string, number> = {};
      for (const row of rows) {
        const currency = row.currency ?? "USD";
        byCurrency[currency] =
          (byCurrency[currency] ?? 0) + Number(row.amount ?? 0);
      }
      return {
        provider,
        amounts: Object.entries(byCurrency)
          .map(([currency, amount]) => ({ currency, amount }))
          .filter((entry) => entry.amount > 0),
        partial: rows.some((row) => row.quality === "partial"),
      };
    });
  }, [costs, apiProviders]);

  const providerSpend = useMemo(() => {
    const map = new Map<string, Map<string, number>>();
    for (const row of activity) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const amount = Number(row.costAmount ?? 0);
      if (!(amount > 0)) continue;
      const provider = row.providerId ?? "unknown";
      const currency = row.costCurrency ?? "USD";
      const byCurrency = map.get(provider) ?? new Map<string, number>();
      byCurrency.set(currency, (byCurrency.get(currency) ?? 0) + amount);
      map.set(provider, byCurrency);
    }
    return map;
  }, [activity, apiProviders]);

  const categoryByProvider = useMemo(
    () => new Map(providers.map((p) => [p.providerId, p.category ?? ""])),
    [providers],
  );

  const modelRows = useMemo(() => {
    const map = new Map<string, ModelRow>();
    for (const row of activity) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const provider = row.providerId ?? "unknown";
      const model = row.modelId ?? "unknown";
      const key = `${provider}\u0000${model}`;
      const entry = map.get(key) ?? {
        provider,
        model,
        sources: [],
        tokens: 0,
        costAmount: row.costAmount,
        costCurrency: row.costCurrency,
      };
      entry.tokens += row.totalTokens ?? 0;
      if (row.sourceId && !entry.sources.includes(row.sourceId)) {
        entry.sources.push(row.sourceId);
      }
      if (entry.costAmount == null && row.costAmount != null) {
        entry.costAmount = row.costAmount;
        entry.costCurrency = row.costCurrency;
      }
      map.set(key, entry);
    }
    return [...map.values()].sort((a, b) => b.tokens - a.tokens);
  }, [activity, apiProviders]);

  const providerRows = useMemo(() => {
    const tokens = new Map<string, number>();
    for (const row of activity) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const provider = row.providerId ?? "unknown";
      tokens.set(provider, (tokens.get(provider) ?? 0) + (row.totalTokens ?? 0));
    }
    return [...tokens.entries()]
      .map(([provider, tokenCount]) => {
        const spend = [...(providerSpend.get(provider)?.entries() ?? [])]
          .map(([currency, amount]) => ({ currency, amount }))
          .filter((entry) => entry.amount > 0);
        return { provider, tokenCount, spend };
      })
      .sort((a, b) => b.tokenCount - a.tokenCount);
  }, [activity, providerSpend, apiProviders]);

  function modelCostCell(row: ModelRow) {
    const category = categoryByProvider.get(row.provider);
    if (category === "subscription") {
      return (
        <span className="dim" title={t.subscriptionNoApiCost}>
          {t.subscription}
        </span>
      );
    }
    if (category === "local_tool") {
      return <span className="dim">—</span>;
    }
    if (row.costAmount != null && Number(row.costAmount) > 0) {
      return (
        <span className="mono">
          {formatAmount(Number(row.costAmount), row.costCurrency ?? "USD")}
        </span>
      );
    }
    return <span className="dim">—</span>;
  }

  const totalTokens = modelRows.reduce((sum, row) => sum + row.tokens, 0);
  const hasCredit = creditCosts.some((c) => c.amounts.length > 0);

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} t={t} />
      {loading ? (
        <Skeleton lines={5} />
      ) : (
        <>
          <div className="metrics">
            <div className="metric">
              <p className="metric-value">
                {spendByCurrency.length > 0
                  ? spendByCurrency
                      .map((s) => formatAmount(s.amount, s.currency))
                      .join(" · ")
                  : "—"}
              </p>
              <p className="metric-label">{t.spend}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{apiProviders.size}</p>
              <p className="metric-label">{t.apiProviders}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{modelRows.length}</p>
              <p className="metric-label">{t.models}</p>
            </div>
            <div className="metric">
              <p className="metric-value">{totalTokens.toLocaleString()}</p>
              <p className="metric-label">{t.tokensCol}</p>
            </div>
          </div>

          <section className="panel">
            <div className="panel-head">
              <h3>{t.apiPaid}</h3>
              <span>{t[PERIOD_LABEL[period]]}</span>
            </div>
            <div className="panel-body">
              <p className="dim panel-hint">{t.apiPaidHint}</p>
              {spendByCurrency.length > 0 ? (
                spendByCurrency.map((s) => (
                  <p key={s.currency} className="mono amount-value">
                    {formatAmount(s.amount, s.currency)}
                  </p>
                ))
              ) : (
                <p className="empty">{t.noSpend}</p>
              )}
            </div>
          </section>

          {hasCredit ? (
            <section className="panel">
              <div className="panel-head">
                <h3>{t.apiCreditEstimate}</h3>
                <span>{t[PERIOD_LABEL[period]]}</span>
              </div>
              <div className="panel-body">
                <p className="dim panel-hint">{t.apiCreditEstimateHint}</p>
                {creditCosts.map((c) => (
                  <div className="credit-row" key={c.provider}>
                    <span className="credit-provider">{c.provider}</span>
                    {c.partial ? (
                      <span className="credit-quality">{t.estimated}</span>
                    ) : null}
                    <span className="mono credit-amount">
                      {c.amounts
                        .map((a) => formatAmount(a.amount, a.currency))
                        .join(" · ")}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          <section className="panel">
            <div className="panel-head">
              <h3>{t.modelDetail}</h3>
              <span>{t[PERIOD_LABEL[period]]}</span>
            </div>
            <div className="panel-body">
              <p className="dim panel-hint">
                {t.apiOnlyNote}{" "}
                <Link className="btn-link" to="/capacity">
                  {t.navCapacity}
                </Link>
              </p>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t.providerCol}</th>
                      <th scope="col">{t.modelCol}</th>
                      <th scope="col" className="num">{t.tokensCol}</th>
                      <th scope="col" className="num">{t.costCol}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modelRows.map((row) => (
                      <tr key={`${row.provider}/${row.model}`}>
                        <th scope="row">{row.provider}</th>
                        <td className="mono">{row.model}</td>
                        <td className="mono num">{row.tokens.toLocaleString()}</td>
                        <td className="mono num">{modelCostCell(row)}</td>
                      </tr>
                    ))}
                    {modelRows.length === 0 ? (
                      <tr>
                        <td colSpan={4} className="empty">
                          {t.noRows}
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h3>{t.providerSummary}</h3>
              <span>{t[PERIOD_LABEL[period]]}</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.providerCol}</th>
                    <th scope="col" className="num">{t.tokensCol}</th>
                    <th scope="col" className="num">{t.spend}</th>
                  </tr>
                </thead>
                <tbody>
                  {providerRows.map((row) => (
                    <tr key={row.provider}>
                      <th scope="row">{row.provider}</th>
                      <td className="mono num">{row.tokenCount.toLocaleString()}</td>
                      <td className="mono num">
                        {row.spend.length > 0
                          ? row.spend
                              .map((s) => formatAmount(s.amount, s.currency))
                              .join(" · ")
                          : "—"}
                      </td>
                    </tr>
                  ))}
                  {providerRows.length === 0 ? (
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
      )}
    </>
  );
}
