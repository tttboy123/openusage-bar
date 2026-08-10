import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowClockwise,
  CaretDown,
  Clock,
  Lightning,
} from "@phosphor-icons/react";
import {
  fetchChanges,
  fetchDecisionTraces,
  fetchRuntimeCapability,
  fetchShouldSendAdvice,
  type ChangeItem,
} from "../api";
import Skeleton from "../components/Skeleton";
import { messages, tpl, type Messages } from "../i18n";
import type { FeatureId, RuntimeCapability } from "../runtimeCapability";
import {
  runtimeCapabilityViewModel,
  type CapabilityTone,
} from "../runtimeCapabilityViewModel";
import { serviceStatusViewModel } from "../serviceStatusViewModel";
import {
  shouldSendAdviceViewModel,
  type ShouldSendAdviceViewModel,
  type ShouldSendDeferTimingViewModel,
  type ShouldSendRequest,
} from "../shouldSendAdvice";
import type {
  DecisionTrace,
  DecisionTraceExclusionReason,
  DecisionTraceFallbackAction,
  DecisionTraceKind,
  DecisionTraceOutcome,
  DecisionTraceReason,
  DecisionTraceStrategy,
} from "../decisionTraces";

type AdvicePhase = "idle" | "loading" | "result" | "error";
type TracePhase = "loading" | "ready" | "empty" | "unavailable" | "unknown";
type AdviceField = "provider" | "model" | "estimatedTokens" | "window";
type AdviceErrorKey =
  | "shouldSendRequired"
  | "shouldSendProviderInvalid"
  | "shouldSendModelInvalid"
  | "shouldSendEstimatedTokensInvalid"
  | "shouldSendWindowInvalid";
type AdviceErrors = Partial<Record<AdviceField, AdviceErrorKey>>;
type AdviceAvailabilityKey =
  | "shouldSendAvailabilityChecking"
  | "shouldSendAvailabilityObserve"
  | "shouldSendAvailabilityStarting"
  | "shouldSendAvailabilityUnavailable"
  | "shouldSendAvailabilityUnsupported"
  | "shouldSendAvailabilityNotConfigured"
  | "shouldSendAvailabilityReady";

interface AdviceDraft {
  provider: string;
  model: string;
  estimatedTokens: string;
  window: string;
}

const INITIAL_ADVICE_DRAFT: AdviceDraft = {
  provider: "",
  model: "",
  estimatedTokens: "",
  window: "5m",
};

function invalidBoundedText(value: string, maximum: number): boolean {
  return (
    value.length > maximum || value !== value.trim() || /\p{C}/u.test(value)
  );
}

function validateAdviceDraft(draft: AdviceDraft): {
  errors: AdviceErrors;
  request: ShouldSendRequest | null;
} {
  const errors: AdviceErrors = {};
  if (!draft.provider) {
    errors.provider = "shouldSendRequired";
  } else if (invalidBoundedText(draft.provider, 128)) {
    errors.provider = "shouldSendProviderInvalid";
  }
  if (!draft.model) {
    errors.model = "shouldSendRequired";
  } else if (invalidBoundedText(draft.model, 256)) {
    errors.model = "shouldSendModelInvalid";
  }
  if (!draft.estimatedTokens) {
    errors.estimatedTokens = "shouldSendRequired";
  } else if (!/^[1-9]\d*$/.test(draft.estimatedTokens)) {
    errors.estimatedTokens = "shouldSendEstimatedTokensInvalid";
  }
  if (!draft.window) {
    errors.window = "shouldSendRequired";
  } else if (invalidBoundedText(draft.window, 64)) {
    errors.window = "shouldSendWindowInvalid";
  }

  const estimatedTokens = Number(draft.estimatedTokens);
  if (
    !errors.estimatedTokens &&
    (!Number.isInteger(estimatedTokens) ||
      estimatedTokens < 1 ||
      estimatedTokens > 2_147_483_647)
  ) {
    errors.estimatedTokens = "shouldSendEstimatedTokensInvalid";
  }
  if (Object.keys(errors).length > 0) return { errors, request: null };
  return {
    errors,
    request: {
      provider: draft.provider,
      model: draft.model,
      estimated_tokens: estimatedTokens,
      window: draft.window,
    },
  };
}

function formatAdviceNumber(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatDeferUntil(value: string, t: Messages): string {
  try {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "—";
    const locale = t === messages.zh ? "zh-CN" : "en-US";
    return new Intl.DateTimeFormat(locale, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(new Date(timestamp));
  } catch {
    return "—";
  }
}

function deferRelativeText(
  t: Messages,
  timing: ShouldSendDeferTimingViewModel,
): string {
  return timing.relativeCount === null
    ? t[timing.relativeKey]
    : tpl(t[timing.relativeKey], { count: timing.relativeCount });
}

function deferTimingAnnouncement(
  t: Messages,
  timing: ShouldSendDeferTimingViewModel,
): string {
  const relative = deferRelativeText(t, timing);
  if (timing.at === null) {
    return `${t.shouldSendDecisionDefer}. ${relative}`;
  }
  return `${t.shouldSendDecisionDefer}. ${t.shouldSendDeferLocalTime}: ${formatDeferUntil(
    timing.at,
    t,
  )}. ${relative}`;
}

function shouldSendAvailability(
  capability: RuntimeCapability | null,
  options: { loading: boolean; refreshing: boolean; error: boolean },
): { available: boolean; messageKey: AdviceAvailabilityKey } {
  if (options.loading || options.refreshing) {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityChecking",
    };
  }
  if (options.error || capability === null) {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityUnavailable",
    };
  }

  const gateway = capability.gateway;
  if (gateway.mode === "observe") {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityObserve",
    };
  }
  if (gateway.operational === "starting") {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityStarting",
    };
  }
  if (
    (gateway.mode !== "advise" && gateway.mode !== "gateway") ||
    (gateway.operational !== "ready" && gateway.operational !== "degraded")
  ) {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityUnavailable",
    };
  }

  const feature = gateway.features.should_send;
  if (feature.support !== "supported") {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityUnsupported",
    };
  }
  if (feature.enabled !== true || feature.configured !== true) {
    return {
      available: false,
      messageKey: "shouldSendAvailabilityNotConfigured",
    };
  }
  return { available: true, messageKey: "shouldSendAvailabilityReady" };
}

function formatChangedAt(at: string | null | undefined): string {
  if (!at) return "—";
  return at.slice(0, 16).replace("T", " ");
}

function pillClass(tone: CapabilityTone): string {
  if (tone === "positive") return "pill-ok";
  if (tone === "warning") return "pill-warn";
  if (tone === "negative") return "pill-bad";
  return "";
}

type TranslationKey = keyof Messages;

const DECISION_TRACE_OUTCOME_KEYS: Record<
  DecisionTraceOutcome,
  TranslationKey
> = {
  yes: "decisionTraceOutcomeYes",
  no: "decisionTraceOutcomeNo",
  defer: "decisionTraceOutcomeDefer",
  selected: "decisionTraceOutcomeSelected",
  unavailable: "decisionTraceOutcomeUnavailable",
  succeeded: "decisionTraceOutcomeSucceeded",
  failed: "decisionTraceOutcomeFailed",
};
const DECISION_TRACE_REASON_KEYS: Record<DecisionTraceReason, TranslationKey> = {
  approaching_limit: "shouldSendReasonApproachingLimit",
  burn_rate_too_high: "shouldSendReasonBurnRateTooHigh",
  quota_healthy: "shouldSendReasonQuotaHealthy",
  quota_low: "shouldSendReasonQuotaLow",
  quota_unknown: "shouldSendReasonQuotaUnknown",
};
const DECISION_TRACE_STRATEGY_KEYS: Record<
  DecisionTraceStrategy,
  TranslationKey
> = {
  "fixed-first": "decisionTraceStrategyFixedFirst",
  "round-robin": "decisionTraceStrategyRoundRobin",
  sticky: "decisionTraceStrategySticky",
  "quota-aware": "decisionTraceStrategyQuotaAware",
  cost: "decisionTraceStrategyCost",
  latency: "decisionTraceStrategyLatency",
  reliability: "decisionTraceStrategyReliability",
};
const DECISION_TRACE_EXCLUSION_KEYS: Record<
  DecisionTraceExclusionReason,
  TranslationKey
> = {
  cooldown: "decisionTraceExclusionCooldown",
  credential_backend_unavailable:
    "decisionTraceExclusionCredentialBackendUnavailable",
  cross_model_unconfirmed: "decisionTraceExclusionCrossModelUnconfirmed",
  cross_provider_unconfirmed:
    "decisionTraceExclusionCrossProviderUnconfirmed",
  cross_region_unconfirmed: "decisionTraceExclusionCrossRegionUnconfirmed",
  disabled: "decisionTraceExclusionDisabled",
  health_unknown: "decisionTraceExclusionHealthUnknown",
  metric_unknown: "decisionTraceExclusionMetricUnknown",
  quota_unknown: "decisionTraceExclusionQuotaUnknown",
  unhealthy: "decisionTraceExclusionUnhealthy",
};
const DECISION_TRACE_FALLBACK_KEYS: Record<
  DecisionTraceFallbackAction,
  TranslationKey
> = {
  none: "decisionTraceFallbackNone",
  retry: "decisionTraceFallbackRetry",
  fail: "decisionTraceFallbackFail",
  degrade_to_cheap: "decisionTraceFallbackDegradeToCheap",
};

function decisionTraceKindKey(kind: DecisionTraceKind): TranslationKey {
  if (kind === "route_advice") return "decisionTraceRouteAdvice";
  if (kind === "gateway_execution") return "decisionTraceGatewayExecution";
  return "decisionTracePoolSelection";
}

function decisionTraceOutcomeKey(outcome: DecisionTraceOutcome): TranslationKey {
  return DECISION_TRACE_OUTCOME_KEYS[outcome];
}

function decisionTraceReasonKey(reason: DecisionTraceReason): TranslationKey {
  return DECISION_TRACE_REASON_KEYS[reason];
}

function decisionTraceStrategyKey(strategy: DecisionTraceStrategy): TranslationKey {
  return DECISION_TRACE_STRATEGY_KEYS[strategy];
}

function decisionTraceExclusionKey(
  reason: DecisionTraceExclusionReason,
): TranslationKey {
  return DECISION_TRACE_EXCLUSION_KEYS[reason];
}

function decisionTraceFallbackKey(
  action: DecisionTraceFallbackAction,
): TranslationKey {
  return DECISION_TRACE_FALLBACK_KEYS[action];
}

function formatDecisionTraceTime(value: string, t: Messages): string {
  try {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "—";
    return new Intl.DateTimeFormat(t === messages.zh ? "zh-CN" : "en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(timestamp));
  } catch {
    return "—";
  }
}

export default function AutomationPage({ t }: { t: Messages }) {
  const [changes, setChanges] = useState<ChangeItem[]>([]);
  const [changesError, setChangesError] = useState(false);
  const [changesLoading, setChangesLoading] = useState(true);
  const [capability, setCapability] = useState<RuntimeCapability | null>(null);
  const [capabilityLoaded, setCapabilityLoaded] = useState(false);
  const [capabilityRefreshing, setCapabilityRefreshing] = useState(false);
  const [capabilityError, setCapabilityError] = useState(false);
  const [expandedFeatures, setExpandedFeatures] = useState<Set<FeatureId>>(
    new Set(),
  );
  const [adviceDraft, setAdviceDraft] = useState<AdviceDraft>(
    INITIAL_ADVICE_DRAFT,
  );
  const [adviceErrors, setAdviceErrors] = useState<AdviceErrors>({});
  const [advicePhase, setAdvicePhase] = useState<AdvicePhase>("idle");
  const [advice, setAdvice] = useState<ShouldSendAdviceViewModel | null>(null);
  const adviceRequestGeneration = useRef(0);
  const [decisionTraces, setDecisionTraces] = useState<DecisionTrace[]>([]);
  const [tracePhase, setTracePhase] = useState<TracePhase>("loading");
  const traceRequestGeneration = useRef(0);

  const loadCapability = useCallback(async () => {
    setCapabilityRefreshing(true);
    try {
      const nextCapability = await fetchRuntimeCapability();
      if (nextCapability === null) {
        setCapabilityError(true);
      } else {
        setCapability(nextCapability);
        setCapabilityError(false);
      }
    } catch {
      setCapabilityError(true);
    } finally {
      setCapabilityLoaded(true);
      setCapabilityRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadCapability();
  }, [loadCapability]);

  const loadDecisionTraces = useCallback(async () => {
    const generation = traceRequestGeneration.current + 1;
    traceRequestGeneration.current = generation;
    setTracePhase("loading");
    try {
      const result = await fetchDecisionTraces();
      if (generation !== traceRequestGeneration.current) return;
      if (result === null) {
        setDecisionTraces([]);
        setTracePhase("unknown");
      } else {
        setDecisionTraces(result.traces);
        setTracePhase(result.traces.length === 0 ? "empty" : "ready");
      }
    } catch {
      if (generation !== traceRequestGeneration.current) return;
      setDecisionTraces([]);
      setTracePhase("unavailable");
    }
  }, []);

  useEffect(() => {
    void loadDecisionTraces();
    return () => {
      traceRequestGeneration.current += 1;
    };
  }, [loadDecisionTraces]);

  useEffect(() => {
    setChangesLoading(true);
    setChangesError(false);
    void fetchChanges(0, 50)
      .then(setChanges)
      .catch(() => setChangesError(true))
      .finally(() => setChangesLoading(false));
  }, []);

  const capabilityLoading = !capabilityLoaded;
  const serviceStatus = serviceStatusViewModel({
    checking: capabilityLoading || capabilityRefreshing,
    failed: capabilityError,
    hasSnapshot: capability !== null,
  });
  const adviceAvailability = shouldSendAvailability(capability, {
    loading: capabilityLoading,
    refreshing: capabilityRefreshing,
    error: capabilityError,
  });
  const adviceFormDisabled =
    !adviceAvailability.available || advicePhase === "loading";

  useEffect(() => {
    if (!adviceAvailability.available) {
      adviceRequestGeneration.current += 1;
      setAdvice(null);
      setAdvicePhase("idle");
    }
  }, [adviceAvailability.available]);

  function toggleFeature(featureId: FeatureId) {
    setExpandedFeatures((current) => {
      const next = new Set(current);
      if (next.has(featureId)) {
        next.delete(featureId);
      } else {
        next.add(featureId);
      }
      return next;
    });
  }

  function updateAdviceField(field: AdviceField, value: string) {
    adviceRequestGeneration.current += 1;
    setAdviceDraft((current) => ({ ...current, [field]: value }));
    setAdviceErrors((current) => {
      if (!(field in current)) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
    setAdvice(null);
    setAdvicePhase("idle");
  }

  async function fetchShouldSendAdviceOnSubmit(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();
    if (!adviceAvailability.available || advicePhase === "loading") return;
    const validated = validateAdviceDraft(adviceDraft);
    setAdviceErrors(validated.errors);
    if (validated.request === null) {
      setAdvice(null);
      setAdvicePhase("idle");
      return;
    }

    setAdvice(null);
    setAdvicePhase("loading");
    const requestGeneration = adviceRequestGeneration.current + 1;
    adviceRequestGeneration.current = requestGeneration;
    try {
      const response = await fetchShouldSendAdvice(validated.request);
      if (requestGeneration !== adviceRequestGeneration.current) return;
      setAdvice(shouldSendAdviceViewModel(response));
      setAdvicePhase("result");
    } catch {
      if (requestGeneration !== adviceRequestGeneration.current) return;
      setAdvice(null);
      setAdvicePhase("error");
    }
  }

  const model = runtimeCapabilityViewModel(capability);
  const adviceAnnouncement =
    advicePhase === "loading"
      ? t.shouldSendChecking
      : advicePhase === "error"
        ? t.shouldSendError
        : advicePhase === "result" && advice
          ? advice.state === "defer" && advice.deferTiming
            ? deferTimingAnnouncement(t, advice.deferTiming)
            : t[advice.statusKey]
          : null;
  const decisionTraceAnnouncement =
    tracePhase === "loading"
      ? t.decisionTraceLoading
      : tracePhase === "empty"
        ? t.decisionTraceEmpty
        : tracePhase === "unavailable"
          ? t.decisionTraceUnavailable
          : tracePhase === "unknown"
            ? t.decisionTraceUnknown
            : tpl(t.decisionTraceLoaded, { count: decisionTraces.length });
  return (
    <>
      <div className="service-status-strip">
        <p className="service-status-message">
          {serviceStatus.messageKey
            ? t[serviceStatus.messageKey]
            : t.serviceStatus}
        </p>
        <p
          className="service-status-announcement"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {[
            t[serviceStatus.announcementKey],
            decisionTraceAnnouncement,
            adviceAnnouncement,
          ]
            .filter((value): value is string => value !== null)
            .join(" ")}
        </p>
        <button
          type="button"
          className="icon-btn service-status-action"
          onClick={() => void loadCapability()}
          disabled={serviceStatus.actionDisabled}
          aria-busy={serviceStatus.actionBusy}
        >
          <ArrowClockwise
            size={16}
            className={`service-status-spinner${
              serviceStatus.actionBusy ? " spinning" : ""
            }`}
            aria-hidden="true"
          />
          {t[serviceStatus.actionKey]}
        </button>
      </div>

      {serviceStatus.showSnapshot ? (
        <>
          <section className="panel" aria-labelledby="observer-status-title">
        <div className="panel-head">
          <h3 id="observer-status-title">{t.observerTitle}</h3>
          <span>{t.serviceStatus}</span>
        </div>
        <div className="panel-body">
          {capabilityLoading ? (
            <Skeleton lines={3} />
          ) : (
            <dl className="automation-grid" style={{ margin: 0 }}>
              <div className="automation-row">
                <dt className="automation-label">{t.capabilityOperational}</dt>
                <dd className="automation-value" style={{ margin: 0 }}>
                  <span className={`pill ${pillClass(model.observer.tone)}`}>
                    {t[model.observer.operationalKey]}
                  </span>
                </dd>
              </div>
              <div className="automation-row">
                <dt className="automation-label">{t.observerAccess}</dt>
                <dd className="automation-value" style={{ margin: 0 }}>
                  {t.observerReadOnly}
                </dd>
              </div>
              <div className="automation-row">
                <dt className="automation-label">{t.generated}</dt>
                <dd className="automation-value mono" style={{ margin: 0 }}>
                  {formatChangedAt(model.observer.generatedAtText)}
                </dd>
              </div>
              <div className="automation-row">
                <dt className="automation-label">{t.lastGood}</dt>
                <dd className="automation-value mono" style={{ margin: 0 }}>
                  {formatChangedAt(model.observer.lastGoodAtText)}
                </dd>
              </div>
              <div className="automation-row">
                <dt className="automation-label">{t.dataRevision}</dt>
                <dd className="automation-value mono" style={{ margin: 0 }}>
                  {model.observer.dataRevisionText}
                </dd>
              </div>
              <div className="automation-row">
                <dt className="automation-label">{t.schemaVersion}</dt>
                <dd className="automation-value mono" style={{ margin: 0 }}>
                  {model.observer.schemaVersionText}
                </dd>
              </div>
            </dl>
          )}
        </div>
          </section>

          <section
            className="panel"
            aria-labelledby="gateway-status-title"
            aria-busy={capabilityRefreshing}
          >
        <div className="panel-head">
          <h3 id="gateway-status-title">{t.gatewayTitle}</h3>
          <span>{t.gatewayOptional}</span>
        </div>
        <div className="panel-body">
          {capabilityLoading ? (
            <Skeleton lines={4} />
          ) : (
            <>
              <dl className="automation-grid" style={{ margin: 0 }}>
                <div className="automation-row">
                  <dt className="automation-label">{t.modeLabel}</dt>
                  <dd className="automation-value" style={{ margin: 0 }}>
                    {t[model.gateway.modeKey]}
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">
                    {t.capabilityOperational}
                  </dt>
                  <dd className="automation-value" style={{ margin: 0 }}>
                    <span className={`pill ${pillClass(model.gateway.tone)}`}>
                      {t[model.gateway.overallOperationalKey]}
                    </span>
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">{t.featureListener}</dt>
                  <dd className="automation-value" style={{ margin: 0 }}>
                    <span
                      className={`pill ${pillClass(model.gateway.listenerTone)}`}
                    >
                      {t[model.gateway.listenerOperationalKey]}
                    </span>
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">{t.configuredProviders}</dt>
                  <dd className="automation-value mono" style={{ margin: 0 }}>
                    {model.gateway.configuredProviderCountText}
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">{t.healthyProviders}</dt>
                  <dd className="automation-value mono" style={{ margin: 0 }}>
                    {model.gateway.healthyProviderCountText}
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">{t.cacheEnabled}</dt>
                  <dd className="automation-value" style={{ margin: 0 }}>
                    {t[model.gateway.cacheEnabledKey]}
                  </dd>
                </div>
                <div className="automation-row">
                  <dt className="automation-label">{t.lastErrorLabel}</dt>
                  <dd className="automation-value" style={{ margin: 0 }}>
                    {model.gateway.lastErrorMessageKey
                      ? t[model.gateway.lastErrorMessageKey]
                      : "—"}
                  </dd>
                </div>
              </dl>

              <p className="health-summary" style={{ marginTop: 14 }}>
                {t[model.gateway.consequenceKey]}
              </p>

              <div className="health-actions" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => void loadCapability()}
                  disabled={capabilityRefreshing}
                  aria-busy={capabilityRefreshing}
                >
                  <ArrowClockwise
                    size={16}
                    className={capabilityRefreshing ? "spinning" : ""}
                    aria-hidden="true"
                  />
                  {t.retryGatewayStatus}
                </button>
              </div>

              <div
                className="should-send-card"
                aria-labelledby="should-send-title"
              >
                <h4 id="should-send-title" className="section-title">
                  {t.shouldSendTitle}
                </h4>
                <p id="should-send-hint" className="health-summary">
                  {t.shouldSendHint}
                </p>
                <p
                  id="should-send-availability"
                  className="should-send-availability"
                >
                  {t[adviceAvailability.messageKey]}
                </p>

                <form
                  className="should-send-form"
                  onSubmit={fetchShouldSendAdviceOnSubmit}
                  aria-describedby="should-send-hint should-send-availability"
                  noValidate
                >
                  <div className="should-send-fields">
                    <div className="should-send-field">
                      <label className="field-label" htmlFor="advice-provider">
                        {t.shouldSendProvider}
                      </label>
                      <input
                        id="advice-provider"
                        className="text-input"
                        type="text"
                        value={adviceDraft.provider}
                        maxLength={128}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={adviceFormDisabled}
                        onChange={(event) =>
                          updateAdviceField("provider", event.currentTarget.value)
                        }
                        aria-invalid={Boolean(adviceErrors.provider)}
                        aria-describedby={`should-send-hint should-send-availability advice-provider-hint${
                          adviceErrors.provider ? " advice-provider-error" : ""
                        }`}
                      />
                      <p
                        id="advice-provider-hint"
                        className="should-send-field-hint"
                      >
                        {t.shouldSendProviderHint}
                      </p>
                      {adviceErrors.provider ? (
                        <p
                          id="advice-provider-error"
                          className="should-send-field-error"
                        >
                          {t[adviceErrors.provider]}
                        </p>
                      ) : null}
                    </div>

                    <div className="should-send-field">
                      <label className="field-label" htmlFor="advice-model">
                        {t.shouldSendModel}
                      </label>
                      <input
                        id="advice-model"
                        className="text-input"
                        type="text"
                        value={adviceDraft.model}
                        maxLength={256}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={adviceFormDisabled}
                        onChange={(event) =>
                          updateAdviceField("model", event.currentTarget.value)
                        }
                        aria-invalid={Boolean(adviceErrors.model)}
                        aria-describedby={`should-send-hint should-send-availability advice-model-hint${
                          adviceErrors.model ? " advice-model-error" : ""
                        }`}
                      />
                      <p
                        id="advice-model-hint"
                        className="should-send-field-hint"
                      >
                        {t.shouldSendModelHint}
                      </p>
                      {adviceErrors.model ? (
                        <p
                          id="advice-model-error"
                          className="should-send-field-error"
                        >
                          {t[adviceErrors.model]}
                        </p>
                      ) : null}
                    </div>

                    <div className="should-send-field">
                      <label className="field-label" htmlFor="advice-tokens">
                        {t.shouldSendEstimatedTokens}
                      </label>
                      <input
                        id="advice-tokens"
                        className="text-input"
                        type="number"
                        inputMode="numeric"
                        min={1}
                        max={2_147_483_647}
                        step={1}
                        value={adviceDraft.estimatedTokens}
                        disabled={adviceFormDisabled}
                        onChange={(event) =>
                          updateAdviceField(
                            "estimatedTokens",
                            event.currentTarget.value,
                          )
                        }
                        aria-invalid={Boolean(adviceErrors.estimatedTokens)}
                        aria-describedby={`should-send-hint should-send-availability advice-tokens-hint${
                          adviceErrors.estimatedTokens
                            ? " advice-tokens-error"
                            : ""
                        }`}
                      />
                      <p
                        id="advice-tokens-hint"
                        className="should-send-field-hint"
                      >
                        {t.shouldSendEstimatedTokensHint}
                      </p>
                      {adviceErrors.estimatedTokens ? (
                        <p
                          id="advice-tokens-error"
                          className="should-send-field-error"
                        >
                          {t[adviceErrors.estimatedTokens]}
                        </p>
                      ) : null}
                    </div>

                    <div className="should-send-field">
                      <label className="field-label" htmlFor="advice-window">
                        {t.shouldSendWindow}
                      </label>
                      <input
                        id="advice-window"
                        className="text-input"
                        type="text"
                        value={adviceDraft.window}
                        maxLength={64}
                        autoComplete="off"
                        spellCheck={false}
                        readOnly
                        disabled={adviceFormDisabled}
                        aria-invalid={Boolean(adviceErrors.window)}
                        aria-describedby={`should-send-hint should-send-availability advice-window-hint${
                          adviceErrors.window ? " advice-window-error" : ""
                        }`}
                      />
                      <p
                        id="advice-window-hint"
                        className="should-send-field-hint"
                      >
                        {t.shouldSendWindowHint}
                      </p>
                      {adviceErrors.window ? (
                        <p
                          id="advice-window-error"
                          className="should-send-field-error"
                        >
                          {t[adviceErrors.window]}
                        </p>
                      ) : null}
                    </div>
                  </div>

                  <div className="should-send-actions">
                    <button
                      type="submit"
                      className="primary-btn"
                      disabled={adviceFormDisabled}
                      aria-busy={advicePhase === "loading"}
                      aria-describedby="should-send-availability"
                    >
                      {advicePhase === "loading"
                        ? t.shouldSendChecking
                        : advicePhase === "error" || advice?.state === "unknown"
                          ? t.shouldSendRetry
                          : t.shouldSendCheck}
                    </button>
                  </div>
                </form>

                <div
                  className={`should-send-result should-send-result-${
                    advice?.state ?? advicePhase
                  }`}
                >
                  {advicePhase === "loading" ? (
                    <p>{t.shouldSendChecking}</p>
                  ) : advicePhase === "error" ? (
                    <>
                      <span className="pill pill-bad">
                        {t.shouldSendDecisionUnknown}
                      </span>
                      <p>{t.shouldSendError}</p>
                    </>
                  ) : advicePhase === "result" && advice ? (
                    <>
                      <span
                        className={`pill ${pillClass(advice.tone)}`}
                      >
                        {advice.state === "yes"
                          ? t.shouldSendDecisionYes
                          : advice.state === "no"
                            ? t.shouldSendDecisionNo
                            : advice.state === "defer"
                              ? t.shouldSendDecisionDefer
                              : t.shouldSendDecisionUnknown}
                      </span>
                      <p className="should-send-reason">
                        <strong>{t.shouldSendReasonLabel}:</strong>{" "}
                        {t[advice.reasonKey]}
                      </p>
                      {advice.state === "defer" && advice.deferTiming ? (
                        <div className="should-send-defer-timing">
                          <h5>{t.shouldSendDeferTimingTitle}</h5>
                          {advice.deferTiming.at ? (
                            <p className="should-send-defer-time">
                              <strong>{t.shouldSendDeferLocalTime}:</strong>{" "}
                              <time dateTime={advice.deferTiming.at}>
                                {formatDeferUntil(advice.deferTiming.at, t)}
                              </time>
                            </p>
                          ) : null}
                          <p className="should-send-defer-relative">
                            {deferRelativeText(t, advice.deferTiming)}
                          </p>
                        </div>
                      ) : null}
                      <h5>{t.shouldSendLocalFactsTitle}</h5>
                      <dl className="should-send-facts">
                        <div>
                          <dt>{t.shouldSendQuotaRemaining}</dt>
                          <dd>
                            {advice.quotaRemaining !== null
                              ? formatAdviceNumber(advice.quotaRemaining)
                              : t.shouldSendLocalFactsInsufficient}
                          </dd>
                        </div>
                        <div>
                          <dt>{t.shouldSendBurnRate}</dt>
                          <dd>
                            {advice.burnRatePerMinute !== null
                              ? `${formatAdviceNumber(
                                  advice.burnRatePerMinute,
                                )} ${t.shouldSendTokensPerMinute}`
                              : t.shouldSendLocalFactsInsufficient}
                          </dd>
                        </div>
                        <div>
                          <dt>{t.shouldSendPredictedExhaustion}</dt>
                          <dd>
                            {advice.predictedExhaustionMinutes !== null
                              ? `${formatAdviceNumber(
                                  advice.predictedExhaustionMinutes,
                                )} ${t.shouldSendMinutes}`
                              : t.shouldSendLocalFactsInsufficient}
                          </dd>
                        </div>
                      </dl>
                      {advice.predictedExhaustionMinutes !== null ? (
                        <p className="should-send-estimate-note">
                          {t.shouldSendEstimateNote}
                        </p>
                      ) : null}
                    </>
                  ) : null}
                </div>
              </div>

              <h4 className="section-title" style={{ marginTop: 18 }}>
                {t.gatewayCapabilities}
              </h4>
              <ul className="health-list">
                {model.gateway.features.map((feature) => {
                  const expanded = expandedFeatures.has(feature.id);
                  const detailId = `gateway-capability-${feature.id}`;
                  return (
                    <li
                      key={feature.id}
                      className={`health-item${expanded ? " open" : ""}`}
                    >
                      <button
                        type="button"
                        className="health-head"
                        aria-expanded={expanded}
                        aria-controls={detailId}
                        onClick={() => toggleFeature(feature.id)}
                      >
                        <span className="health-title">
                          {t[feature.labelKey]}
                        </span>
                        <span style={{ flex: 1 }} />
                        <span className={`pill ${pillClass(feature.tone)}`}>
                          {t[feature.operationalKey]}
                        </span>
                        <CaretDown
                          className="health-chevron"
                          size={14}
                          aria-hidden="true"
                        />
                      </button>
                      <div
                        className="health-detail"
                        id={detailId}
                        hidden={!expanded}
                      >
                        <div>
                          <div className="health-inner">
                            <dl className="health-facts">
                              <div>
                                <dt>{t.capabilitySupport}</dt>
                                <dd>{t[feature.supportKey]}</dd>
                              </div>
                              <div>
                                <dt>{t.capabilityEnabled}</dt>
                                <dd>{t[feature.enabledKey]}</dd>
                              </div>
                              <div>
                                <dt>{t.capabilityConfigured}</dt>
                                <dd>{t[feature.configuredKey]}</dd>
                              </div>
                              <div>
                                <dt>{t.capabilityOperational}</dt>
                                <dd>{t[feature.operationalKey]}</dd>
                              </div>
                            </dl>
                          </div>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
          </section>
        </>
      ) : null}

      <section
        className="panel decision-trace-card"
        aria-labelledby="decision-trace-title"
        aria-busy={tracePhase === "loading"}
      >
        <div className="panel-head">
          <h3 id="decision-trace-title">{t.decisionTraceTitle}</h3>
          <span>{t.gatewayOptional}</span>
        </div>
        <div className="panel-body">
          <div className="decision-trace-toolbar">
            <p className="decision-trace-runtime-note">
              {t.decisionTraceRuntimeOnly}
            </p>
            <button
              type="button"
              className="icon-btn"
              onClick={() => void loadDecisionTraces()}
              disabled={tracePhase === "loading"}
              aria-busy={tracePhase === "loading"}
            >
              <ArrowClockwise
                size={16}
                className={tracePhase === "loading" ? "spinning" : ""}
                aria-hidden="true"
              />
              {t.decisionTraceRefresh}
            </button>
          </div>
          {tracePhase === "loading" ? (
            <div className="decision-trace-state">
              <Skeleton lines={3} />
              <p>{t.decisionTraceLoading}</p>
            </div>
          ) : tracePhase === "ready" ? (
            <ol className="decision-trace-list">
              {decisionTraces.map((trace) => (
                <li key={trace.traceId} className="decision-trace-item">
                  <article>
                    <div className="decision-trace-head">
                      <h4>{t[decisionTraceKindKey(trace.kind)]}</h4>
                      <time dateTime={trace.occurredAt}>
                        <Clock size={13} aria-hidden="true" />
                        {formatDecisionTraceTime(trace.occurredAt, t)}
                      </time>
                    </div>
                    <p className="decision-trace-execution">
                      <span
                        className={`pill ${
                          trace.execution === "advice_only"
                            ? "pill-neutral"
                            : "pill-ok"
                        }`}
                      >
                        {trace.execution === "advice_only"
                          ? t.decisionTraceAdviceOnly
                          : t.decisionTraceExecuted}
                      </span>
                    </p>

                    <dl className="decision-trace-facts">
                      <div>
                        <dt>{t.decisionTraceOutcome}</dt>
                        <dd className="decision-trace-value">
                          {t[decisionTraceOutcomeKey(trace.outcome)]}
                        </dd>
                      </div>
                      {trace.reason !== null ? (
                        <div>
                          <dt>{t.shouldSendReasonLabel}</dt>
                          <dd className="decision-trace-value">
                            {t[decisionTraceReasonKey(trace.reason)]}
                          </dd>
                        </div>
                      ) : null}
                    </dl>

                    {trace.pool !== null ? (
                      <section
                        className="decision-trace-section"
                        aria-label={t.decisionTracePool}
                      >
                        <h5>{t.decisionTracePool}</h5>
                        <dl className="decision-trace-facts">
                          <div>
                            <dt>{t.decisionTracePool}</dt>
                            <dd className="decision-trace-value mono">
                              {trace.pool.poolId}
                            </dd>
                          </div>
                          <div>
                            <dt>{t.decisionTraceRevision}</dt>
                            <dd className="decision-trace-value mono">
                              {trace.pool.revision.toLocaleString()}
                            </dd>
                          </div>
                          <div>
                            <dt>{t.decisionTraceStrategy}</dt>
                            <dd className="decision-trace-value">
                              {t[decisionTraceStrategyKey(trace.pool.strategy)]}
                            </dd>
                          </div>
                        </dl>
                      </section>
                    ) : null}

                    {trace.selected !== null ? (
                      <section
                        className="decision-trace-section"
                        aria-label={t.decisionTraceSelectedTarget}
                      >
                        <h5>{t.decisionTraceSelectedTarget}</h5>
                        <dl className="decision-trace-facts">
                          {trace.selected.providerId !== null ? (
                            <div>
                              <dt>{t.decisionTraceSelectedProvider}</dt>
                              <dd className="decision-trace-value mono">
                                {trace.selected.providerId}
                              </dd>
                            </div>
                          ) : null}
                          {trace.selected.accountDisplayId !== null ? (
                            <div>
                              <dt>{t.decisionTraceSelectedAccount}</dt>
                              <dd className="decision-trace-value mono">
                                {trace.selected.accountDisplayId}
                              </dd>
                            </div>
                          ) : null}
                        </dl>
                      </section>
                    ) : null}

                    {trace.exclusions.length > 0 ? (
                      <section
                        className="decision-trace-section"
                        aria-label={t.decisionTraceExclusions}
                      >
                        <h5>{t.decisionTraceExclusions}</h5>
                        <ul className="decision-trace-exclusions">
                          {trace.exclusions.map((exclusion, index) => (
                            <li key={`${exclusion.accountDisplayId}-${index}`}>
                              <span className="decision-trace-value mono">
                                {exclusion.accountDisplayId}
                              </span>
                              <span>
                                {t[decisionTraceExclusionKey(exclusion.reason)]}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </section>
                    ) : null}

                    {trace.fallback !== null ? (
                      <section
                        className="decision-trace-section"
                        aria-label={t.decisionTraceFallback}
                      >
                        <h5>{t.decisionTraceFallback}</h5>
                        <dl className="decision-trace-facts">
                          <div>
                            <dt>{t.decisionTraceFallbackAttempted}</dt>
                            <dd className="decision-trace-value">
                              {trace.fallback.attempted ? t.valueYes : t.valueNo}
                            </dd>
                          </div>
                          <div>
                            <dt>{t.decisionTraceFallbackAttempts}</dt>
                            <dd className="decision-trace-value mono">
                              {trace.fallback.attemptCount}
                            </dd>
                          </div>
                          <div>
                            <dt>{t.decisionTraceFallbackFinalAction}</dt>
                            <dd className="decision-trace-value">
                              {t[
                                decisionTraceFallbackKey(
                                  trace.fallback.finalAction,
                                )
                              ]}
                            </dd>
                          </div>
                        </dl>
                      </section>
                    ) : null}

                    {trace.factsWindow !== null ? (
                      <section
                        className="decision-trace-section"
                        aria-label={t.decisionTraceFactsWindow}
                      >
                        <h5>{t.decisionTraceFactsWindow}</h5>
                        <p className="decision-trace-value mono">
                          {tpl(t.decisionTraceSeconds, {
                            count: trace.factsWindow.durationSeconds,
                          })}
                        </p>
                      </section>
                    ) : null}
                  </article>
                </li>
              ))}
            </ol>
          ) : (
            <p className={`decision-trace-state decision-trace-${tracePhase}`}>
              {tracePhase === "empty"
                ? t.decisionTraceEmpty
                : tracePhase === "unavailable"
                  ? t.decisionTraceUnavailable
                  : t.decisionTraceUnknown}
            </p>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>
            <Lightning
              size={16}
              style={{ marginRight: 6, verticalAlign: -2 }}
              aria-hidden="true"
            />
            {t.navAutomation}
          </h3>
          <span>v0.8</span>
        </div>
        <div className="panel-body">
          <p className="dim" style={{ margin: "0 0 12px", fontSize: "0.74rem" }}>
            {t.changeFeedHint}
          </p>
          {changesLoading ? (
            <Skeleton lines={3} />
          ) : changes.length > 0 ? (
            <ul className="change-list">
              {changes.map((item, index) => (
                <li key={item.changeSeq ?? index} className="change-item">
                  <span className="change-seq">#{item.changeSeq ?? "—"}</span>
                  <span className="change-type">
                    {item.recordType ?? "unknown"}
                  </span>
                  <span className="change-record">{item.recordId ?? "—"}</span>
                  <span className="change-time">
                    <Clock
                      size={12}
                      style={{ marginRight: 4, verticalAlign: -1 }}
                      aria-hidden="true"
                    />
                    {formatChangedAt(item.changedAt)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">
              {changesError ? t.changeFeedUnavailable : t.noChanges}
            </p>
          )}
        </div>
      </section>
    </>
  );
}
