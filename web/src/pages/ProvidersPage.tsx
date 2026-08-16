import { useCallback, useEffect, useState } from "react";
import {
  Plus,
  HardDrives,
  ArrowClockwise,
  PencilSimple,
  Trash,
  SquaresFour,
  List as ListIcon,
} from "@phosphor-icons/react";
import {
  applyProviderConfig,
  fetchProviders,
  fetchSources,
  fetchQuickConnect,
  fetchProviderConfigPresets,
  type ProviderItem,
  type SourceItem,
  type QuickConnectItem,
  type ProviderConfigPreset,
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
import {
  buildGatewayAccountIntent,
  createBrowserGatewayAccountHostAdapter,
  normalizeGatewayAccountCapability,
  normalizeGatewayAccountLaunchError,
  normalizeGatewayAccountOperationResponse,
  normalizeGatewayAccountOperationStatus,
  type GatewayProviderPreset,
  type GatewayAccountAction,
  type GatewayAccountHostAdapter,
  type GatewayAccountOperationFailureCode,
  type GatewayAccountOperationResponse,
  type GatewayAccountOperationStatus,
} from "../gatewayAccountActions";
import AddProviderDialog from "../components/AddProviderDialog";
import {
  AccountPoolEditorDialog,
  AccountPoolRemoveDialog,
} from "../components/AccountPoolDialogs";
import {
  GatewayAccountCreateDialog,
  ProviderAccountActions,
} from "../components/ProviderAccountActions";
import ProviderCard from "../components/ProviderCard";
import { type Messages, tpl } from "../i18n";

const ACCOUNT_POOLS_PATH = "/gateway/v1/account-pools";
const DEFAULT_HOST_ADAPTER = createBrowserAccountPoolHostAdapter();
const DEFAULT_GATEWAY_ACCOUNT_HOST_ADAPTER = createBrowserGatewayAccountHostAdapter();

type HostAvailability = "checking" | "trusted" | "readOnly";
type PoolFeedback = "saved" | "removed" | "refreshFailed" | null;
type EditorState = {
  mode: "create" | "edit";
  pool: AccountPoolDefinition | null;
} | null;

export default function ProvidersPage({
  t,
  accountPoolHostAdapter = DEFAULT_HOST_ADAPTER,
  gatewayAccountHostAdapter = DEFAULT_GATEWAY_ACCOUNT_HOST_ADAPTER,
}: {
  t: Messages;
  accountPoolHostAdapter?: AccountPoolHostAdapter;
  gatewayAccountHostAdapter?: GatewayAccountHostAdapter;
}) {
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [quick, setQuick] = useState<QuickConnectItem[]>([]);
  const [providerPresets, setProviderPresets] = useState<ProviderConfigPreset[]>([]);
  const [providerConfigCanApply, setProviderConfigCanApply] = useState(false);
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
  const [gatewayAccountHostAvailability, setGatewayAccountHostAvailability] =
    useState<HostAvailability>("checking");
  const [gatewayAccountCreateOpen, setGatewayAccountCreateOpen] = useState(false);
  const [gatewayAccountLaunchPending, setGatewayAccountLaunchPending] = useState(false);
  const [gatewayAccountOperation, setGatewayAccountOperation] = useState<
    GatewayAccountOperationResponse | GatewayAccountOperationStatus | null
  >(null);
  const [gatewayAccountOperationError, setGatewayAccountOperationError] = useState<
    GatewayAccountOperationFailureCode | "invalid_intent" | null
  >(null);
  const [gatewayPublicListRefreshFailed, setGatewayPublicListRefreshFailed] = useState(false);
  const [layout, setLayout] = useState<"grid" | "list">(() => {
    try {
      return localStorage.getItem("usagehub.providers.layout") === "list" ? "list" : "grid";
    } catch {
      return "grid";
    }
  });

  const load = async (quiet = false) => {
    if (!quiet) setIsRefreshing(true);
    try {
      const [p, s, q, presets, poolResult] = await Promise.all([
        fetchProviders(),
        fetchSources(),
        fetchQuickConnect(),
        fetchProviderConfigPresets().then(
          (value) => value,
          () => [] as ProviderConfigPreset[],
        ),
        fetchAccountPoolsSnapshot().then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const, value: null }),
        ),
      ]);
      setProviders(p);
      setSources(s);
      setQuick(q);
      setProviderPresets(presets);
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
    const controller = new AbortController();
    fetch("/host/v1/capabilities", {
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((value) => {
        if (controller.signal.aborted) return;
        const actions =
          value !== null &&
          typeof value === "object" &&
          Array.isArray((value as { actions?: unknown }).actions)
            ? ((value as { actions: string[] }).actions ?? [])
            : [];
        setProviderConfigCanApply(actions.includes("providerConfig.apply"));
      })
      .catch(() => {
        if (!controller.signal.aborted) setProviderConfigCanApply(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setGatewayAccountHostAvailability("checking");
    gatewayAccountHostAdapter.readCapability(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          setGatewayAccountHostAvailability(
            normalizeGatewayAccountCapability(value) === null
              ? "readOnly"
              : "trusted",
          );
        }
      },
      () => {
        if (!controller.signal.aborted) setGatewayAccountHostAvailability("readOnly");
      },
    );
    return () => controller.abort();
  }, [gatewayAccountHostAdapter]);

  useEffect(() => {
    const id = setInterval(() => load(true), 60000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem("usagehub.providers.layout", layout);
    } catch {
      // Preference persistence is best-effort only.
    }
  }, [layout]);

  const sourcesByProvider = new Map(
    sources.map((s) => [s.providerId, s] as const),
  );
  const quickByFamily = new Map(quick.map((q) => [q.familyId, q]));
  const accountPools = accountPoolsViewModel(accountPoolsSnapshot);
  const gatewayAccountBusy =
    gatewayAccountLaunchPending ||
    gatewayAccountOperation?.state === "opened" ||
    gatewayAccountOperation?.state === "pending";

  const refreshAccountPools = useCallback(async () => {
    const value = await fetchAccountPoolsSnapshot();
    if (normalizeAccountPools(value) === null) throw new Error("invalid account pool snapshot");
    setAccountPoolsSnapshot(value);
  }, []);

  async function launchGatewayAccountIntent(
    action: GatewayAccountAction,
    target: { preset: GatewayProviderPreset } | { displayId: string },
  ) {
    if (
      gatewayAccountHostAvailability !== "trusted" ||
      gatewayAccountLaunchPending
    ) {
      return;
    }
    const intent = buildGatewayAccountIntent(action, target);
    if (intent === null) {
      setGatewayAccountOperationError("invalid_intent");
      return;
    }
    setGatewayAccountLaunchPending(true);
    setGatewayAccountOperation(null);
    setGatewayAccountOperationError(null);
    setGatewayPublicListRefreshFailed(false);
    try {
      const raw = await gatewayAccountHostAdapter.launch(intent);
      const launchError = normalizeGatewayAccountLaunchError(raw);
      if (launchError !== null) {
        setGatewayAccountOperationError(launchError.error.code);
        return;
      }
      const opened = normalizeGatewayAccountOperationResponse(raw);
      if (opened === null) {
        setGatewayAccountOperationError("service_unavailable");
        return;
      }
      setGatewayAccountOperation(opened);
      setGatewayAccountCreateOpen(false);
    } catch {
      setGatewayAccountOperationError("service_unavailable");
    } finally {
      setGatewayAccountLaunchPending(false);
    }
  }

  useEffect(() => {
    if (
      gatewayAccountOperation === null ||
      (gatewayAccountOperation.state !== "opened" &&
        gatewayAccountOperation.state !== "pending")
    ) {
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      gatewayAccountHostAdapter
        .readOperation(gatewayAccountOperation.operationId, controller.signal)
        .then(async (value) => {
          if (controller.signal.aborted) return;
          const status = normalizeGatewayAccountOperationStatus(value);
          if (
            status === null ||
            status.operationId !== gatewayAccountOperation.operationId
          ) {
            setGatewayAccountOperation(null);
            setGatewayAccountOperationError("service_unavailable");
            return;
          }
          setGatewayAccountOperation(status);
          setGatewayAccountOperationError(
            status.state === "failed" ? status.code : null,
          );
          if (status.state === "succeeded") {
            try {
              await refreshAccountPools();
              setGatewayPublicListRefreshFailed(false);
            } catch {
              setGatewayPublicListRefreshFailed(true);
            }
          }
        }, () => {
          if (!controller.signal.aborted) {
            setGatewayAccountOperation(null);
            setGatewayAccountOperationError("service_unavailable");
          }
        });
    }, 1200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [gatewayAccountHostAdapter, gatewayAccountOperation, refreshAccountPools]);

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
          {t.browseProviderPresets}
        </button>
        <div
          className="segmented"
          role="group"
          aria-label={t.providerLayoutLabel}
        >
          <button
            type="button"
            aria-pressed={layout === "grid"}
            onClick={() => setLayout("grid")}
          >
            <SquaresFour size={14} />
            {t.layoutGrid}
          </button>
          <button
            type="button"
            aria-pressed={layout === "list"}
            onClick={() => setLayout("list")}
          >
            <ListIcon size={14} />
            {t.layoutList}
          </button>
        </div>
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
        gatewayAccountHostAvailability={gatewayAccountHostAvailability}
        gatewayAccountBusy={gatewayAccountBusy}
        feedback={poolFeedback}
        gatewayAccountOperation={gatewayAccountOperation}
        gatewayAccountOperationError={gatewayAccountOperationError}
        gatewayPublicListRefreshFailed={gatewayPublicListRefreshFailed}
        onCreateGatewayAccount={() => setGatewayAccountCreateOpen(true)}
        onGatewayAccountAction={(action, displayId) =>
          launchGatewayAccountIntent(action, { displayId })
        }
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
        presets={providerPresets}
        onClose={() => setDialogOpen(false)}
        t={t}
        canApply={providerConfigCanApply}
        apply={applyProviderConfig}
      />
      {gatewayAccountCreateOpen ? (
        <GatewayAccountCreateDialog
          pending={gatewayAccountLaunchPending}
          onCancel={() => {
            if (!gatewayAccountLaunchPending) setGatewayAccountCreateOpen(false);
          }}
          onContinue={(preset) =>
            launchGatewayAccountIntent("gatewayAccount.openCreate", { preset })
          }
          t={t}
        />
      ) : null}
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
      <section
        className={layout === "list" ? "provider-list" : "provider-grid"}
        aria-label={t.navProviders}
      >
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
           variant={layout}
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
  gatewayAccountHostAvailability,
  gatewayAccountBusy,
  feedback,
  gatewayAccountOperation,
  gatewayAccountOperationError,
  gatewayPublicListRefreshFailed,
  onCreateGatewayAccount,
  onGatewayAccountAction,
  onCreate,
  onEdit,
  onRemove,
  onRetryRefresh,
}: {
  t: Messages;
  model: ReturnType<typeof accountPoolsViewModel>;
  hostAvailability: HostAvailability;
  gatewayAccountHostAvailability: HostAvailability;
  gatewayAccountBusy: boolean;
  feedback: PoolFeedback;
  gatewayAccountOperation: GatewayAccountOperationResponse | GatewayAccountOperationStatus | null;
  gatewayAccountOperationError: GatewayAccountOperationFailureCode | "invalid_intent" | null;
  gatewayPublicListRefreshFailed: boolean;
  onCreateGatewayAccount: () => void;
  onGatewayAccountAction: (
    action: Exclude<GatewayAccountAction, "gatewayAccount.openCreate">,
    displayId: string,
  ) => void;
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
        <div className="account-pools-head-actions">
          {gatewayAccountHostAvailability === "trusted" ? (
            <button
              type="button"
              className="secondary-btn gateway-account-create"
              disabled={gatewayAccountBusy}
              onClick={onCreateGatewayAccount}
            >
              {t.addGatewayAccount}
            </button>
          ) : null}
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
      </div>
      <p className="gateway-account-management-note">
        {gatewayAccountHostAvailability === "checking"
          ? t.gatewayAccountChecking
          : gatewayAccountHostAvailability === "trusted"
            ? t.gatewayAccountTrusted
            : t.gatewayAccountReadOnly}
      </p>
      {gatewayAccountOperation || gatewayAccountOperationError || gatewayPublicListRefreshFailed ? (
        <div
          className="gateway-account-operation"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {gatewayAccountOperationError
            ? gatewayAccountOperationErrorText(gatewayAccountOperationError, t)
            : gatewayPublicListRefreshFailed
              ? t.gatewayPublicListRefreshFailed
              : gatewayAccountOperationText(gatewayAccountOperation, t)}
        </div>
      ) : null}
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
                gatewayAccountTrusted={gatewayAccountHostAvailability === "trusted"}
                gatewayAccountBusy={gatewayAccountBusy}
                onGatewayAccountAction={onGatewayAccountAction}
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
  gatewayAccountTrusted,
  gatewayAccountBusy,
  onGatewayAccountAction,
  t,
}: {
  account: AccountPoolAccountViewModel;
  index: number;
  gatewayAccountTrusted: boolean;
  gatewayAccountBusy: boolean;
  onGatewayAccountAction: (
    action: Exclude<GatewayAccountAction, "gatewayAccount.openCreate">,
    displayId: string,
  ) => void;
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
          <dt>{t.providerCol}</dt>
          <dd>{account.providerId}</dd>
        </div>
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
      <ProviderAccountActions
        trusted={gatewayAccountTrusted}
        busy={gatewayAccountBusy}
        displayId={account.displayId}
        onAction={onGatewayAccountAction}
        t={t}
      />
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

function gatewayAccountOperationText(
  operation: GatewayAccountOperationResponse | GatewayAccountOperationStatus | null,
  t: Messages,
): string {
  if (operation === null) return t.gatewayAccountOperationUnavailable;
  if (operation.state === "opened") return t.gatewayAccountOperationOpened;
  if (operation.state === "pending") return t.gatewayAccountOperationPending;
  if (operation.state === "succeeded") return t.gatewayAccountOperationSucceeded;
  if (operation.state === "cancelled") return t.gatewayAccountOperationCancelled;
  if (operation.state === "timed_out") return t.gatewayAccountOperationTimedOut;
  return gatewayAccountOperationErrorText(operation.code, t);
}

function gatewayAccountOperationErrorText(
  code: GatewayAccountOperationFailureCode | "invalid_intent",
  t: Messages,
): string {
  if (code === "account_in_use") return t.gatewayAccountErrorAccountInUse;
  if (code === "not_found") return t.gatewayAccountErrorNotFound;
  if (code === "already_exists") return t.gatewayAccountErrorAlreadyExists;
  if (code === "invalid_intent" || code === "invalid_input") {
    return t.gatewayAccountErrorInvalidInput;
  }
  if (code === "credential_unavailable") {
    return t.gatewayAccountErrorCredentialUnavailable;
  }
  if (code === "config_write_failed") return t.gatewayAccountErrorConfigWriteFailed;
  if (code === "helper_busy") return t.gatewayAccountErrorHelperBusy;
  return t.gatewayAccountOperationUnavailable;
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
  return tpl(days === 1 ? t.dayAgo : t.daysAgo, { count: days });
}
