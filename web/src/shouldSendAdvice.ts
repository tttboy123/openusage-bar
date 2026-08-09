const MAX_PROVIDER_LENGTH = 128;
const MAX_MODEL_LENGTH = 256;
const MAX_WINDOW_LENGTH = 64;
const MAX_ESTIMATED_TOKENS = 2_147_483_647;
const MAX_REQUEST_BYTES = 1_024;

const DECISIONS = new Set(["yes", "no", "defer"] as const);
const REASONS = new Set([
  "approaching_limit",
  "burn_rate_too_high",
  "quota_healthy",
  "quota_low",
  "quota_unknown",
] as const);

export type ShouldSendDecision = "yes" | "no" | "defer";
export type ShouldSendReason =
  | "approaching_limit"
  | "burn_rate_too_high"
  | "quota_healthy"
  | "quota_low"
  | "quota_unknown";

export interface ShouldSendRequest {
  provider: string;
  model: string;
  estimated_tokens: number;
  window: string;
}

export interface ShouldSendAdvice {
  decision: ShouldSendDecision | "unknown";
  reason: ShouldSendReason | "unknown";
  confidence: number | null;
  deferUntil: string | null;
  details: {
    quotaRemaining: number | null;
    burnRatePerMinute: number | null;
    predictedExhaustionMinutes: number | null;
  };
}

export type ShouldSendDeferTimingViewModel =
  | {
      state: "scheduled";
      at: string;
      relativeKey:
        | "shouldSendDeferInMinutes"
        | "shouldSendDeferInHours"
        | "shouldSendDeferInDays";
      relativeCount: number;
    }
  | {
      state: "ready";
      at: string;
      relativeKey: "shouldSendDeferReady";
      relativeCount: null;
    }
  | {
      state: "after_refresh";
      at: null;
      relativeKey: "shouldSendDeferAfterRefresh";
      relativeCount: null;
    };

export type ShouldSendAdviceViewModel = {
  state: ShouldSendDecision | "unknown";
  statusKey:
    | "shouldSendDecisionYes"
    | "shouldSendDecisionNo"
    | "shouldSendDecisionDefer"
    | "shouldSendDecisionUnknown";
  reasonKey:
    | "shouldSendReasonApproachingLimit"
    | "shouldSendReasonBurnRateTooHigh"
    | "shouldSendReasonQuotaHealthy"
    | "shouldSendReasonQuotaLow"
    | "shouldSendReasonQuotaUnknown"
    | "shouldSendReasonUnknown";
  tone: "positive" | "negative" | "warning" | "neutral";
  quotaRemaining: number | null;
  quotaRemainingKey: "shouldSendLocalFactsInsufficient" | null;
  burnRatePerMinute: number | null;
  burnRateKey: "shouldSendLocalFactsInsufficient" | null;
  predictedExhaustionMinutes: number | null;
  predictedExhaustionKey: "shouldSendLocalFactsInsufficient" | null;
  deferTiming: ShouldSendDeferTimingViewModel | null;
};

type OwnRead =
  | { ok: true; value: unknown }
  | { ok: false; value?: never };

const UNKNOWN_ADVICE: ShouldSendAdvice = Object.freeze({
  decision: "unknown",
  reason: "unknown",
  confidence: null,
  deferUntil: null,
  details: Object.freeze({
    quotaRemaining: null,
    burnRatePerMinute: null,
    predictedExhaustionMinutes: null,
  }),
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readOwn(value: Record<string, unknown>, key: string): OwnRead {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !("value" in descriptor)) return { ok: false };
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}

function exactOwnKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  try {
    const keys = Object.keys(value);
    return (
      keys.length === expected.length &&
      expected.every((key) => keys.includes(key))
    );
  } catch {
    return false;
  }
}

function boundedText(value: unknown, maximum: number): string | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value !== value.trim() ||
    /\p{C}/u.test(value)
  ) {
    return null;
  }
  return value;
}

function finiteNonNegative(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function nullableFiniteNonNegative(value: unknown): number | null | undefined {
  if (value === null) return null;
  const number = finiteNonNegative(value);
  return number === null ? undefined : number;
}

function validDeferUntil(value: unknown): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 64 ||
    value !== value.trim()
  ) {
    return null;
  }

  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(
    value,
  );
  if (match === null) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6] ?? "0");
  const offsetHour = Number(match[7] ?? "0");
  const offsetMinute = Number(match[8] ?? "0");
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHour > 23 ||
    offsetMinute > 59
  ) {
    return null;
  }

  const leapYear =
    year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    0,
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  if (day < 1 || day > daysInMonth[month]) return null;
  return value;
}

export function validatedShouldSendRequest(value: unknown): ShouldSendRequest {
  if (!isRecord(value)) throw new TypeError("Invalid Should-Send request.");
  const expected = ["provider", "model", "estimated_tokens", "window"];
  if (!exactOwnKeys(value, expected)) {
    throw new TypeError("Invalid Should-Send request.");
  }

  const providerRead = readOwn(value, "provider");
  const modelRead = readOwn(value, "model");
  const tokensRead = readOwn(value, "estimated_tokens");
  const windowRead = readOwn(value, "window");
  const provider = providerRead.ok
    ? boundedText(providerRead.value, MAX_PROVIDER_LENGTH)
    : null;
  const model = modelRead.ok
    ? boundedText(modelRead.value, MAX_MODEL_LENGTH)
    : null;
  const window = windowRead.ok
    ? boundedText(windowRead.value, MAX_WINDOW_LENGTH)
    : null;
  const estimatedTokens = tokensRead.ok ? tokensRead.value : null;

  if (
    provider === null ||
    model === null ||
    window === null ||
    typeof estimatedTokens !== "number" ||
    !Number.isInteger(estimatedTokens) ||
    estimatedTokens < 1 ||
    estimatedTokens > MAX_ESTIMATED_TOKENS
  ) {
    throw new TypeError("Invalid Should-Send request.");
  }

  return {
    provider,
    model,
    estimated_tokens: estimatedTokens,
    window,
  };
}

export function canonicalShouldSendRequest(value: unknown): string {
  const request = validatedShouldSendRequest(value);
  const body = JSON.stringify({
    provider: request.provider,
    model: request.model,
    estimated_tokens: request.estimated_tokens,
    window: request.window,
  });
  if (new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
    throw new TypeError("Invalid Should-Send request.");
  }
  return body;
}

export function normalizeShouldSendAdvice(value: unknown): ShouldSendAdvice {
  try {
    if (!isRecord(value)) return UNKNOWN_ADVICE;
    const decisionRead = readOwn(value, "decision");
    const confidenceRead = readOwn(value, "confidence");
    const reasonRead = readOwn(value, "reason");
    const deferUntilRead = readOwn(value, "defer_until");
    const detailsRead = readOwn(value, "details");
    if (
      !decisionRead.ok ||
      !confidenceRead.ok ||
      !reasonRead.ok ||
      !detailsRead.ok
    ) {
      return UNKNOWN_ADVICE;
    }

    const decision = decisionRead.value;
    const confidence = finiteNonNegative(confidenceRead.value);
    const reason = reasonRead.value;
    const deferUntil = validDeferUntil(
      deferUntilRead.ok ? deferUntilRead.value : null,
    );
    if (
      typeof decision !== "string" ||
      !DECISIONS.has(decision as ShouldSendDecision) ||
      confidence === null ||
      confidence > 1 ||
      typeof reason !== "string" ||
      !REASONS.has(reason as ShouldSendReason) ||
      !isRecord(detailsRead.value)
    ) {
      return UNKNOWN_ADVICE;
    }

    const quotaRead = readOwn(detailsRead.value, "quota_remaining");
    const burnRead = readOwn(detailsRead.value, "burn_rate_per_min");
    const predictedRead = readOwn(
      detailsRead.value,
      "predicted_exhaustion_minutes",
    );
    if (!quotaRead.ok || !burnRead.ok || !predictedRead.ok) {
      return UNKNOWN_ADVICE;
    }
    const quotaRemaining = nullableFiniteNonNegative(quotaRead.value);
    const burnRatePerMinute = nullableFiniteNonNegative(burnRead.value);
    const predictedExhaustionMinutes = nullableFiniteNonNegative(
      predictedRead.value,
    );
    if (
      quotaRemaining === undefined ||
      burnRatePerMinute === undefined ||
      predictedExhaustionMinutes === undefined
    ) {
      return UNKNOWN_ADVICE;
    }

    return {
      decision: decision as ShouldSendDecision,
      reason: reason as ShouldSendReason,
      confidence,
      deferUntil: decision === "defer" ? deferUntil : null,
      details: {
        quotaRemaining,
        burnRatePerMinute,
        predictedExhaustionMinutes,
      },
    };
  } catch {
    return UNKNOWN_ADVICE;
  }
}

const STATUS_KEYS: Record<ShouldSendDecision, ShouldSendAdviceViewModel["statusKey"]> = {
  yes: "shouldSendDecisionYes",
  no: "shouldSendDecisionNo",
  defer: "shouldSendDecisionDefer",
};

const REASON_KEYS: Record<ShouldSendReason, ShouldSendAdviceViewModel["reasonKey"]> = {
  approaching_limit: "shouldSendReasonApproachingLimit",
  burn_rate_too_high: "shouldSendReasonBurnRateTooHigh",
  quota_healthy: "shouldSendReasonQuotaHealthy",
  quota_low: "shouldSendReasonQuotaLow",
  quota_unknown: "shouldSendReasonQuotaUnknown",
};

const TONES: Record<ShouldSendDecision, ShouldSendAdviceViewModel["tone"]> = {
  yes: "positive",
  no: "negative",
  defer: "warning",
};

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function deferTimingViewModel(
  deferUntil: unknown,
  nowMs: number,
): ShouldSendDeferTimingViewModel {
  const at = validDeferUntil(deferUntil);
  if (typeof at !== "string") {
    return {
      state: "after_refresh",
      at: null,
      relativeKey: "shouldSendDeferAfterRefresh",
      relativeCount: null,
    };
  }

  const atMs = Date.parse(at);
  const effectiveNowMs = Number.isFinite(nowMs) ? nowMs : Date.now();
  const remainingMs = atMs - effectiveNowMs;
  if (remainingMs <= 0) {
    return {
      state: "ready",
      at,
      relativeKey: "shouldSendDeferReady",
      relativeCount: null,
    };
  }
  if (remainingMs < 90 * MINUTE_MS) {
    return {
      state: "scheduled",
      at,
      relativeKey: "shouldSendDeferInMinutes",
      relativeCount: Math.ceil(remainingMs / MINUTE_MS),
    };
  }
  if (remainingMs < 36 * HOUR_MS) {
    return {
      state: "scheduled",
      at,
      relativeKey: "shouldSendDeferInHours",
      relativeCount: Math.ceil(remainingMs / HOUR_MS),
    };
  }
  return {
    state: "scheduled",
    at,
    relativeKey: "shouldSendDeferInDays",
    relativeCount: Math.ceil(remainingMs / DAY_MS),
  };
}

export function shouldSendAdviceViewModel(
  advice: ShouldSendAdvice,
  nowMs = Date.now(),
): ShouldSendAdviceViewModel {
  if (
    advice.decision === "unknown" ||
    advice.reason === "unknown" ||
    !DECISIONS.has(advice.decision) ||
    !REASONS.has(advice.reason)
  ) {
    return {
      state: "unknown",
      statusKey: "shouldSendDecisionUnknown",
      reasonKey: "shouldSendReasonUnknown",
      tone: "neutral",
      quotaRemaining: null,
      quotaRemainingKey: "shouldSendLocalFactsInsufficient",
      burnRatePerMinute: null,
      burnRateKey: "shouldSendLocalFactsInsufficient",
      predictedExhaustionMinutes: null,
      predictedExhaustionKey: "shouldSendLocalFactsInsufficient",
      deferTiming: null,
    };
  }

  return {
    state: advice.decision,
    statusKey: STATUS_KEYS[advice.decision],
    reasonKey: REASON_KEYS[advice.reason],
    tone: TONES[advice.decision],
    quotaRemaining: advice.details.quotaRemaining,
    quotaRemainingKey:
      advice.details.quotaRemaining === null
        ? "shouldSendLocalFactsInsufficient"
        : null,
    burnRatePerMinute: advice.details.burnRatePerMinute,
    burnRateKey:
      advice.details.burnRatePerMinute === null
        ? "shouldSendLocalFactsInsufficient"
        : null,
    predictedExhaustionMinutes:
      advice.details.predictedExhaustionMinutes,
    predictedExhaustionKey:
      advice.details.predictedExhaustionMinutes === null
        ? "shouldSendLocalFactsInsufficient"
        : null,
    deferTiming:
      advice.decision === "defer"
        ? deferTimingViewModel(advice.deferUntil, nowMs)
        : null,
  };
}
