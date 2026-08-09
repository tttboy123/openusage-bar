import { useEffect, useState } from "react";
import { Plus, HardDrives, ArrowClockwise } from "@phosphor-icons/react";
import {
  fetchProviders,
  fetchSources,
  fetchQuickConnect,
  type ProviderItem,
  type SourceItem,
  type QuickConnectItem,
} from "../api";
import {
  accountPoolsViewModel,
  type AccountPoolAccountViewModel,
  type AccountPoolStatusTone,
} from "../accountPools";
import AddProviderDialog from "../components/AddProviderDialog";
import ProviderCard from "../components/ProviderCard";
import { type Messages, tpl } from "../i18n";

const ACCOUNT_POOLS_PATH = "/gateway/v1/account-pools";

export default function ProvidersPage({ t }: { t: Messages }) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [accountPoolsSnapshot, setAccountPoolsSnapshot] = useState<unknown>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const load = async (quiet = false) => {
    if (!quiet) setIsRefreshing(true);
    try {
      const [p, s, q, poolResult] = await Promise.all([
        fetchProviders(),
        fetchSources(),
        fetchQuickConnect(),
        fetchAccountPoolsSnapshot().then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const, value: null }),
        ),
      ]);
      setProviders(p);
      setSources(s);
      setQuick(q);
      if (poolResult.ok) setAccountPoolsSnapshot(poolResult.value);
      setError(null);
      setLastRefreshed(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      if (!quiet) setIsRefreshing(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const id = setInterval(() => load(true), 60000);
    return () => clearInterval(id);
  }, []);

  const sourcesByProvider = new Map(
    sources.map((s) => [s.providerId, s] as const),
  );
  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));
  const accountPools = accountPoolsViewModel(accountPoolsSnapshot);

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
        <div className="toolbar-right">
          <span className="dim">
            {lastRefreshed
              ? tpl(t.lastUpdated, { time: relativeTime(lastRefreshed.toISOString(), t) })
              : t.refreshStatus}
          </span>
          <button
            type="button"
            className="icon-btn"
            onClick={() => load()}
            disabled={isRefreshing}
            aria-label={t.refresh}
            title={t.refresh}
          >
            <ArrowClockwise size={16} className={isRefreshing ? "spinning" : ""} />
          </button>
        </div>
      </div>
      <AccountPoolsSection t={t} model={accountPools} />
      <AddProviderDialog
        open={dialogOpen}
        presets={quick}
        onClose={() => setDialogOpen(false)}
        t={t}
      />
      <section className="provider-grid" aria-label={t.navProviders}>
        {providers.map((item) => (
         <ProviderCard
           key={item.providerId}
           providerId={item.providerId ?? ""}
           displayName={item.displayName ?? item.providerId ?? "n/a"}
           familyId={item.familyId ?? item.providerId ?? ""}
           sourceKind={item.sourceKind}
           source={sourcesByProvider.get(item.providerId ?? "")}
           quick={quickByFamily.get(item.familyId ?? "")}
           t={t}
            isSyncing={isRefreshing}
         />
        ))}
        {providers.length === 0 && !error ? (
          <div className="empty-block" style={{ gridColumn: "1 / -1" }}>
            <HardDrives size={32} />
            <p>{t.comingSoon}</p>
          </div>
        ) : null}
      </section>
      {error ? <p className="empty">{error}</p> : null}
    </>
  );
}

function AccountPoolsSection({
  t,
  model,
}: {
  t: Messages;
  model: ReturnType<typeof accountPoolsViewModel>;
}) {
  const describedBy = "account-pools-note";
  const summary = tpl(t.accountPoolsSummary, {
    total: model.summary.total,
    disabled: model.summary.disabled,
    backend: model.summary.backendUnavailable,
    unknown: model.summary.unknown,
  });

  return (
    <section
      className="account-pools-panel"
      aria-labelledby="account-pools-title"
      aria-describedby={describedBy}
    >
      <div className="account-pools-head">
        <div>
          <h3 id="account-pools-title">{t.accountPoolsTitle}</h3>
          <p id={describedBy} className="account-pools-note">
            {t.accountPoolsPrivacyNote}
          </p>
        </div>
        <button
          type="button"
          className="secondary-btn account-pools-create"
          aria-disabled="true"
          aria-describedby="account-pool-create-note"
          onClick={(event) => event.preventDefault()}
        >
          {t.createPool}
        </button>
      </div>
      <p id="account-pool-create-note" className="account-pools-create-note">
        {t.createPoolUnverified}
      </p>
      {model.state === "ready" ? (
        <>
          <p className="account-pools-summary">{summary}</p>
          <div className="account-pools-list" role="list">
            {model.accounts.map((account, index) => (
              <AccountPoolCard
                key={`${account.displayId}:${index}`}
                account={account}
                index={index}
                t={t}
              />
            ))}
          </div>
        </>
      ) : (
        <div
          className={`account-pools-empty account-pools-empty-${model.state}`}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {model.state === "empty"
            ? t.accountPoolsEmpty
            : t.accountPoolsUnknown}
        </div>
      )}
    </section>
  );
}

function AccountPoolCard({
  account,
  index,
  t,
}: {
  account: AccountPoolAccountViewModel;
  index: number;
  t: Messages;
}) {
  const titleId = `account-pool-card-title-${index}`;
  const detailId = `account-pool-card-detail-${index}`;
  const displayName = account.alias ?? account.displayId;
  const poolText =
    account.pools.length === 0
      ? t.accountPoolsNoMembership
      : account.pools
          .map((pool) =>
            tpl(t.accountPoolMembership, {
              pool: pool.poolId,
              priority: pool.priority,
              weight: pool.weight,
            }),
          )
          .join(" · ");

  return (
    <article
      className={`account-pool-card account-pool-card-${account.statusTone}`}
      role="listitem"
      tabIndex={0}
      aria-labelledby={titleId}
      aria-describedby={detailId}
    >
      <div className="account-pool-card-main">
        <div className="account-pool-identity">
          <h4 id={titleId}>{displayName}</h4>
          <span className="account-pool-display-id">{account.displayId}</span>
        </div>
        <span className={`provider-status ${poolStatusClass(account.statusTone)}`}>
          <span className="provider-status-dot" aria-hidden="true" />
          {t[account.statusKey]}
        </span>
      </div>
      <dl id={detailId} className="account-pool-facts">
        <div>
          <dt>{t.priority}</dt>
          <dd>{account.priority}</dd>
        </div>
        <div>
          <dt>{t.weight}</dt>
          <dd>{account.weight}</dd>
        </div>
        <div>
          <dt>{t.cooldown}</dt>
          <dd>{cooldownText(account, t)}</dd>
        </div>
        <div>
          <dt>{t.quotaState}</dt>
          <dd>{quotaText(account, t)}</dd>
        </div>
      </dl>
      <p className="account-pool-memberships">{poolText}</p>
    </article>
  );
}

function poolStatusClass(tone: AccountPoolStatusTone): string {
  if (tone === "positive") return "provider-status-ok";
  if (tone === "warning") return "provider-status-warn";
  if (tone === "negative") return "provider-status-bad";
  if (tone === "unknown") return "provider-status-neutral account-pool-status-unknown";
  return "provider-status-neutral";
}

function cooldownText(account: AccountPoolAccountViewModel, t: Messages): string {
  if (account.cooldown.state === "active") {
    return account.cooldown.until
      ? tpl(t.cooldownUntil, { time: relativeTime(account.cooldown.until, t) })
      : t.cooldownActive;
  }
  if (account.cooldown.state === "inactive") return t.cooldownInactive;
  return t.valueUnknown;
}

function quotaText(account: AccountPoolAccountViewModel, t: Messages): string {
  if (account.quota.state === "available") {
    if (account.quota.remaining !== null && account.quota.limit !== null) {
      return tpl(t.quotaRemainingOfLimit, {
        remaining: formatCount(account.quota.remaining),
        limit: formatCount(account.quota.limit),
      });
    }
    return t.accountPoolQuotaAvailableUnverified;
  }
  if (account.quota.state === "exhausted") return t.accountPoolQuotaExhausted;
  return t.accountPoolQuotaUnknown;
}

function formatCount(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
}

async function fetchAccountPoolsSnapshot(): Promise<unknown> {
  const response = await fetch(ACCOUNT_POOLS_PATH, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) return null;
  return response.json();
}

function relativeTime(date: string | null | undefined, t: Messages): string {
  if (!date) return t.never;
  const then = new Date(date).getTime();
  if (Number.isNaN(then)) return t.never;
  const minutes = Math.floor((Date.now() - then) / 60000);
  if (minutes < 1) return t.justNow ?? "just now";
  if (minutes < 60) return tpl(t.minutesAgo, { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return tpl(t.hoursAgo, { count: hours });
  const days = Math.floor(hours / 24);
  return tpl(t.daysAgo, { count: days });
}
