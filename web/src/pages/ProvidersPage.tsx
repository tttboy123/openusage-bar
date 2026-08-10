import { useCallback, useEffect, useState } from "react";
import { Plus, HardDrives, ArrowClockwise, PencilSimple, Trash } from "@phosphor-icons/react";
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
  normalizeAccountPools,
  type AccountPoolAccountViewModel,
  type AccountPoolDefinition,
  type AccountPoolStatusTone,
} from "../accountPools";
import {
  buildPoolHostAction,
  createBrowserAccountPoolHostAdapter,
  normalizePoolHostActionResult,
  normalizeTrustedHostCapability,
  type AccountPoolHostAction,
  type AccountPoolHostAdapter,
  type EditablePool,
  type PoolHostActionFailureCode,
} from "../accountPoolActions";
import AddProviderDialog from "../components/AddProviderDialog";
import {
  AccountPoolEditorDialog,
  AccountPoolRemoveDialog,
} from "../components/AccountPoolDialogs";
import ProviderCard from "../components/ProviderCard";
import { type Messages, tpl } from "../i18n";

const ACCOUNT_POOLS_PATH = "/gateway/v1/account-pools";
const DEFAULT_HOST_ADAPTER = createBrowserAccountPoolHostAdapter();

type HostAvailability = "checking" | "trusted" | "readOnly";
type PoolFeedback = "saved" | "removed" | "refreshFailed" | null;
type EditorState = {
  mode: "create" | "edit";
  pool: AccountPoolDefinition | null;
} | null;

export default function ProvidersPage({
  t,
  accountPoolHostAdapter = DEFAULT_HOST_ADAPTER,
}: {
  t: Messages;
  accountPoolHostAdapter?: AccountPoolHostAdapter;
}) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [accountPoolsSnapshot, setAccountPoolsSnapshot] = useState<unknown>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [hostAvailability, setHostAvailability] = useState<HostAvailability>("checking");
  const [editor, setEditor] = useState<EditorState>(null);
  const [poolToRemove, setPoolToRemove] = useState<AccountPoolDefinition | null>(null);
  const [mutationPending, setMutationPending] = useState(false);
  const [mutationError, setMutationError] = useState<PoolHostActionFailureCode | null>(null);
  const [poolFeedback, setPoolFeedback] = useState<PoolFeedback>(null);

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
    const controller = new AbortController();
    setHostAvailability("checking");
    accountPoolHostAdapter.readCapability(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          setHostAvailability(
            normalizeTrustedHostCapability(value) === null ? "readOnly" : "trusted",
          );
        }
      },
      () => {
        if (!controller.signal.aborted) setHostAvailability("readOnly");
      },
    );
    return () => controller.abort();
  }, [accountPoolHostAdapter]);

  useEffect(() => {
    const id = setInterval(() => load(true), 60000);
    return () => clearInterval(id);
  }, []);

  const sourcesByProvider = new Map(
    sources.map((s) => [s.providerId, s] as const),
  );
  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));
  const accountPools = accountPoolsViewModel(accountPoolsSnapshot);

  const refreshAccountPools = useCallback(async () => {
    const value = await fetchAccountPoolsSnapshot();
    if (normalizeAccountPools(value) === null) throw new Error("invalid account pool snapshot");
    setAccountPoolsSnapshot(value);
  }, []);

  async function mutatePool(
    action: AccountPoolHostAction,
    pool: EditablePool | { poolId: string },
    expectedRevision: number | null,
  ) {
    if (hostAvailability !== "trusted" || mutationPending) return;
    const request = buildPoolHostAction(action, pool, expectedRevision);
    if (request === null) {
      setMutationError("invalid_request");
      return;
    }
    setMutationPending(true);
    setMutationError(null);
    setPoolFeedback(null);
    try {
      const raw = await accountPoolHostAdapter.mutate(request);
      const result = normalizePoolHostActionResult(raw);
      if (result === null || result.action !== action) {
        setMutationError("service_unavailable");
        return;
      }
      if (!result.ok) {
        setMutationError(result.code);
        return;
      }
      setEditor(null);
      setPoolToRemove(null);
      try {
        await refreshAccountPools();
        setPoolFeedback(action === "accountPool.remove" ? "removed" : "saved");
      } catch {
        setPoolFeedback("refreshFailed");
      }
    } catch {
      setMutationError("service_unavailable");
    } finally {
      setMutationPending(false);
    }
  }

  async function retryPoolRefresh() {
    try {
      await refreshAccountPools();
      setPoolFeedback(null);
    } catch {
      setPoolFeedback("refreshFailed");
    }
  }

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
      <AccountPoolsSection
        t={t}
        model={accountPools}
        hostAvailability={hostAvailability}
        feedback={poolFeedback}
        onCreate={() => {
          setMutationError(null);
          setEditor({ mode: "create", pool: null });
        }}
        onEdit={(pool) => {
          setMutationError(null);
          setEditor({ mode: "edit", pool });
        }}
        onRemove={(pool) => {
          setMutationError(null);
          setPoolToRemove(pool);
        }}
        onRetryRefresh={retryPoolRefresh}
      />
      <AddProviderDialog
        open={dialogOpen}
        presets={quick}
        onClose={() => setDialogOpen(false)}
        t={t}
      />
      {editor ? (
        <AccountPoolEditorDialog
          key={`${editor.mode}:${editor.pool?.poolId ?? "new"}:${editor.pool?.revision ?? 0}`}
          mode={editor.mode}
          accounts={accountPools.accounts}
          pool={editor.pool}
          pending={mutationPending}
          error={mutationError ? poolMutationError(mutationError, t) : null}
          onCancel={() => {
            if (!mutationPending) setEditor(null);
          }}
          onSubmit={(pool) =>
            mutatePool(
              editor.mode === "create" ? "accountPool.create" : "accountPool.edit",
              pool,
              editor.pool?.revision ?? null,
            )
          }
          t={t}
        />
      ) : null}
      {poolToRemove ? (
        <AccountPoolRemoveDialog
          pool={poolToRemove}
          pending={mutationPending}
          error={mutationError ? poolMutationError(mutationError, t) : null}
          onCancel={() => {
            if (!mutationPending) setPoolToRemove(null);
          }}
          onConfirm={() =>
            mutatePool(
              "accountPool.remove",
              { poolId: poolToRemove.poolId },
              poolToRemove.revision,
            )
          }
          t={t}
        />
      ) : null}
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
  hostAvailability,
  feedback,
  onCreate,
  onEdit,
  onRemove,
  onRetryRefresh,
}: {
  t: Messages;
  model: ReturnType<typeof accountPoolsViewModel>;
  hostAvailability: HostAvailability;
  feedback: PoolFeedback;
  onCreate: () => void;
  onEdit: (pool: AccountPoolDefinition) => void;
  onRemove: (pool: AccountPoolDefinition) => void;
  onRetryRefresh: () => void;
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
          disabled={hostAvailability !== "trusted" || model.accounts.length === 0}
          aria-describedby="account-pool-create-note"
          onClick={onCreate}
        >
          {t.createPool}
        </button>
      </div>
      <p id="account-pool-create-note" className="account-pools-create-note">
        {hostAvailability === "trusted"
          ? model.accounts.length === 0
            ? t.accountPoolsNoAccounts
            : t.accountPoolsTrusted
          : t.accountPoolsReadOnly}
      </p>
      {feedback ? (
        <div className="account-pool-feedback" role="status" aria-live="polite" aria-atomic="true">
          <span>
            {feedback === "saved"
              ? t.poolSaved
              : feedback === "removed"
                ? t.poolRemoved
                : t.poolRefreshFailed}
          </span>
          {feedback === "refreshFailed" ? (
            <button type="button" className="btn-link" onClick={onRetryRefresh}>
              {t.retryPoolRefresh}
            </button>
          ) : null}
        </div>
      ) : null}
      {model.state === "ready" ? (
        <>
          <p className="account-pools-summary">{summary}</p>
          <div className="account-pool-definitions-head">
            <h4>{t.poolDefinitionsTitle}</h4>
          </div>
          {model.pools.length > 0 ? (
            <div className="account-pool-definitions" role="list">
              {model.pools.map((pool) => (
                <AccountPoolDefinitionCard
                  key={`${pool.poolId}:${pool.revision}`}
                  pool={pool}
                  accounts={model.accounts}
                  trusted={hostAvailability === "trusted"}
                  onEdit={() => onEdit(pool)}
                  onRemove={() => onRemove(pool)}
                  t={t}
                />
              ))}
            </div>
          ) : (
            <p className="account-pools-empty">{t.poolDefinitionsEmpty}</p>
          )}
          <div className="account-pools-list" role="list">
            {model.accounts.map((account, index) => (
              <AccountPoolAccountCard
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

function AccountPoolDefinitionCard({
  pool,
  accounts,
  trusted,
  onEdit,
  onRemove,
  t,
}: {
  pool: AccountPoolDefinition;
  accounts: AccountPoolAccountViewModel[];
  trusted: boolean;
  onEdit: () => void;
  onRemove: () => void;
  t: Messages;
}) {
  const aliases = new Map(
    accounts.map((account) => [account.displayId, account.alias ?? account.displayId]),
  );
  const fallback = tpl(t.poolFallbackSummary, {
    provider: pool.crossProviderFallback ? t.fallbackAllowed : t.fallbackBlocked,
    model: pool.crossModelFallback ? t.fallbackAllowed : t.fallbackBlocked,
    region: pool.crossRegionFallback ? t.fallbackAllowed : t.fallbackBlocked,
  });
  return (
    <article className="account-pool-definition-card" role="listitem">
      <div className="account-pool-card-main">
        <div className="account-pool-identity">
          <h5>{pool.poolId}</h5>
          <span className="account-pool-display-id">{pool.strategy} · {t.poolRevision} {pool.revision}</span>
        </div>
        {trusted ? (
          <div className="account-pool-definition-actions">
            <button type="button" className="icon-btn" onClick={onEdit} aria-label={`${t.editPool}: ${pool.poolId}`}>
              <PencilSimple size={15} aria-hidden="true" />
              {t.editPool}
            </button>
            <button type="button" className="icon-btn" onClick={onRemove} aria-label={`${t.removePool}: ${pool.poolId}`}>
              <Trash size={15} aria-hidden="true" />
              {t.removePool}
            </button>
          </div>
        ) : null}
      </div>
      <ul className="account-pool-definition-members">
        {pool.members.map((member) => (
          <li key={member.displayId}>
            <span>{aliases.get(member.displayId) ?? member.displayId}</span>
            <span>{t.priority} {member.priority} · {t.weight} {member.weight}</span>
          </li>
        ))}
      </ul>
      <p className="account-pool-memberships">{fallback}</p>
    </article>
  );
}

function AccountPoolAccountCard({
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

function poolMutationError(code: PoolHostActionFailureCode, t: Messages): string {
  if (code === "revision_conflict") return t.poolRevisionConflict;
  if (code === "already_exists") return t.poolAlreadyExists;
  if (code === "not_found") return t.poolNotFound;
  if (code === "pool_references_unknown_account") return t.poolUnknownAccount;
  if (code === "invalid_request") return t.poolMutationInvalid;
  return t.poolMutationUnavailable;
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
