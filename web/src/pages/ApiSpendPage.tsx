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
  return `${currency} ${amount.toFixed(4)}`;
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
        setActivity(a);
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

  const totals = useMemo(() => {
    const byCurrency: Record<string, number> = {};
    for (const row of costs) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const currency = row.currency ?? "USD";
      byCurrency[currency] =
        (byCurrency[currency] ?? 0) + Number(row.amount ?? 0);
    }
    return Object.entries(byCurrency)
      .map(([currency, amount]) => ({ currency, amount }))
      .filter((entry) => entry.amount > 0);
  }, [costs, apiProviders]);

  const providerCost = useMemo(() => {
    const map = new Map<string, Map<string, number>>();
    for (const row of costs) {
      if (!apiProviders.has(row.providerId ?? "")) continue;
      const provider = row.providerId ?? "unknown";
      const currency = row.currency ?? "USD";
      const byCurrency = map.get(provider) ?? new Map<string, number>();
      byCurrency.set(
        currency,
        (byCurrency.get(currency) ?? 0) + Number(row.amount ?? 0),
      );
      map.set(provider, byCurrency);
    }
    return map;
  }, [costs, apiProviders]);

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
        const amounts = [...(providerCost.get(provider)?.entries() ?? [])].filter(
          ([, amount]) => amount > 0,
        );
        return {
          provider,
          tokenCount,
          amounts: amounts.map(([currency, amount]) =>
            formatAmount(amount, currency),
          ),
        };
      })
      .sort((a, b) => b.tokenCount - a.tokenCount);
  }, [activity, providerCost, apiProviders]);

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
    const amounts = [...(providerCost.get(row.provider)?.entries() ?? [])].filter(
      ([, amount]) => amount > 0,
    );
    if (amounts.length === 0) {
      return <span className="dim">—</span>;
    }
    return (
      <span className="mono dim" title={t.providerLevelCost}>
        ≈ {amounts.map(([currency, amount]) => formatAmount(amount, currency)).join(" · ")}
      </span>
    );
  }

  function categoryLabel(category: string | undefined): string {
    if (category === "subscription") return t.subscription;
    if (category === "local_tool") return t.localTool;
    return t.apiPaid;
  }

  return (
    <>
      <PeriodSelector value={period} onChange={setPeriod} t={t} />
      {loading ? (
        <Skeleton lines={5} />
      ) : (
        <>
          <section className="panel">
            <div className="panel-head">
              <h3>{t.apiPaid}</h3>
              <span>{t[PERIOD_LABEL[period]]}</span>
            </div>
            <div className="panel-body">
              <p className="dim" style={{ margin: "0 0 10px", fontSize: "0.74rem" }}>
                {t.apiPaidHint}
              </p>
              {totals.map((total) => (
                <p
                  key={total.currency}
                  className="mono"
                  style={{ fontSize: "1.2rem", margin: 0 }}
                >
                  {formatAmount(total.amount, total.currency)}
                </p>
              ))}
              {totals.length === 0 ? <p className="empty">{t.noSpend}</p> : null}
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h3>{t.modelDetail}</h3>
              <span>{t[PERIOD_LABEL[period]]}</span>
            </div>
            <div className="panel-body">
              <p className="dim" style={{ margin: 0, fontSize: "0.74rem" }}>
                {t.apiOnlyNote}{" "}
                <Link className="btn-link" to="/capacity">
                  {t.navCapacity}
                </Link>
              </p>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.providerCol}</th>
                    <th scope="col">{t.modelCol}</th>
                    <th scope="col">{t.sourceCol}</th>
                    <th scope="col">{t.tokensCol}</th>
                    <th scope="col">{t.amountCol}</th>
                  </tr>
                </thead>
                <tbody>
                  {modelRows.map((row) => (
                    <tr key={`${row.provider}/${row.model}`}>
                      <th scope="row">{row.provider}</th>
                      <td className="mono">{row.model}</td>
                      <td className="mono dim">{row.sources.join(", ")}</td>
                      <td className="mono">{row.tokens.toLocaleString()}</td>
                      <td>{modelCostCell(row)}</td>
                    </tr>
                  ))}
                  {modelRows.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="empty">
                        {t.noSpend}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
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
                    <th scope="col">{t.categoryCol}</th>
                    <th scope="col">{t.tokensCol}</th>
                    <th scope="col">{t.amountCol}</th>
                  </tr>
                </thead>
                <tbody>
                  {providerRows.map((row) => (
                    <tr key={row.provider}>
                      <th scope="row">{row.provider}</th>
                      <td>{categoryLabel(categoryByProvider.get(row.provider))}</td>
                      <td className="mono">{row.tokenCount.toLocaleString()}</td>
                      <td className="mono">
                        {categoryByProvider.get(row.provider) === "subscription" ? (
                          <span className="dim">{t.subscriptionNoApiCost}</span>
                        ) : row.amounts.length > 0 ? (
                          row.amounts.join(" · ")
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                  {providerRows.length === 0 ? (
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
          {error ? <p className="empty">{error}</p> : null}
        </>
      )}
    </>
  );
}
