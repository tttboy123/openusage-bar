import type { Messages } from "./i18n";
import {
  normalizeRuntimeCapability,
  type CapabilityActionId,
  type FeatureId,
  type GatewayMode,
  type KnownBoolean,
  type Operational,
  type RuntimeCapability,
  type SafeErrorCode,
  type Support,
} from "./runtimeCapability";

type MessageKey = keyof Messages;
export type CapabilityTone = "positive" | "warning" | "negative" | "neutral";

export interface RuntimeFeatureViewModel {
  id: FeatureId;
  labelKey: MessageKey;
  supportKey: MessageKey;
  enabledKey: MessageKey;
  configuredKey: MessageKey;
  operationalKey: MessageKey;
  tone: CapabilityTone;
}

export interface RuntimeCapabilityViewModel {
  observer: {
    titleKey: MessageKey;
    operationalKey: MessageKey;
    tone: CapabilityTone;
    generatedAtText: string;
    lastGoodAtText: string;
    dataRevisionText: string;
    schemaVersionText: string;
  };
  gateway: {
    titleKey: MessageKey;
    optionalKey: MessageKey;
    modeKey: MessageKey;
    overallOperationalKey: MessageKey;
    tone: CapabilityTone;
    listenerOperationalKey: MessageKey;
    listenerTone: CapabilityTone;
    configuredProviderCountText: string;
    healthyProviderCountText: string;
    cacheEnabledKey: MessageKey;
    lastErrorMessageKey: MessageKey | null;
    consequenceKey: MessageKey;
    actions: CapabilityActionId[];
    features: RuntimeFeatureViewModel[];
  };
}

const UNKNOWN_TEXT = "—";

const OPERATIONAL_KEY: Record<Operational, MessageKey> = {
  disabled: "operationalDisabled",
  starting: "operationalStarting",
  ready: "operationalReady",
  degraded: "operationalDegraded",
  unavailable: "operationalUnavailable",
  unknown: "operationalUnknown",
};

const MODE_KEY: Record<GatewayMode, MessageKey> = {
  observe: "modeObserve",
  advise: "modeAdvise",
  gateway: "modeGateway",
  unknown: "modeUnknown",
};

const SUPPORT_KEY: Record<Support, MessageKey> = {
  supported: "supportSupported",
  unsupported: "supportUnsupported",
  unknown: "supportUnknown",
};

const ERROR_KEY: Record<SafeErrorCode, MessageKey> = {
  gateway_disabled: "gatewayErrorDisabled",
  gateway_starting: "gatewayErrorStarting",
  gateway_timeout: "gatewayErrorTimeout",
  gateway_unavailable: "gatewayErrorUnavailable",
  provider_unavailable: "gatewayErrorProviderUnavailable",
  credential_backend_unavailable: "gatewayErrorCredentialBackendUnavailable",
  capability_invalid: "gatewayErrorCapabilityInvalid",
};

const FEATURE_PRESENTATION: ReadonlyArray<{
  id: FeatureId;
  labelKey: MessageKey;
}> = [
  { id: "listener", labelKey: "featureListener" },
  { id: "should_send", labelKey: "featureShouldSend" },
  { id: "responses", labelKey: "featureResponses" },
  { id: "pii_redaction", labelKey: "featurePiiRedaction" },
  { id: "streaming", labelKey: "featureStreaming" },
  { id: "cache", labelKey: "featureCache" },
  { id: "fallback", labelKey: "featureFallback" },
];

export function runtimeCapabilityViewModel(
  snapshot: unknown,
): RuntimeCapabilityViewModel {
  const unknownErrorCode = hasUnknownErrorCode(snapshot);
  const capability = normalizeRuntimeCapability(snapshot);
  const observerOperational = capability?.observer.operational ?? "unknown";
  const gatewayMode = capability?.gateway.mode ?? "unknown";
  const gatewayOperational = capability?.gateway.operational ?? "unknown";
  const listenerOperational =
    capability?.gateway.features.listener.operational ?? "unknown";
  const cacheEnabled = capability?.gateway.features.cache.enabled ?? "unknown";

  const features = FEATURE_PRESENTATION.map(({ id, labelKey }) => {
    const feature = capability?.gateway.features[id];
    const operational = feature?.operational ?? "unknown";
    return {
      id,
      labelKey,
      supportKey: SUPPORT_KEY[feature?.support ?? "unknown"],
      enabledKey: booleanKey(feature?.enabled ?? "unknown"),
      configuredKey: booleanKey(feature?.configured ?? "unknown"),
      operationalKey: OPERATIONAL_KEY[operational],
      tone: operationalTone(operational),
    };
  });

  return {
    observer: {
      titleKey: "observerTitle",
      operationalKey: OPERATIONAL_KEY[observerOperational],
      tone: operationalTone(observerOperational),
      generatedAtText: safeText(capability, "generatedAt"),
      lastGoodAtText: safeText(capability, "lastGoodAt"),
      dataRevisionText: safeCount(capability?.observer.dataRevision),
      schemaVersionText: capability?.observer.schemaVersion ?? UNKNOWN_TEXT,
    },
    gateway: {
      titleKey: "gatewayTitle",
      optionalKey: "gatewayOptional",
      modeKey: MODE_KEY[gatewayMode],
      overallOperationalKey: OPERATIONAL_KEY[gatewayOperational],
      tone: operationalTone(gatewayOperational),
      listenerOperationalKey: OPERATIONAL_KEY[listenerOperational],
      listenerTone: operationalTone(listenerOperational),
      configuredProviderCountText: safeCount(
        capability?.gateway.configuredProviderCount,
      ),
      healthyProviderCountText: safeCount(
        capability?.gateway.healthyProviderCount,
      ),
      cacheEnabledKey: booleanKey(cacheEnabled),
      lastErrorMessageKey: lastErrorKey(capability, unknownErrorCode),
      consequenceKey: consequenceKey(gatewayMode, gatewayOperational),
      actions: ["retry"],
      features,
    },
  };
}

function safeText(
  capability: RuntimeCapability | null,
  field: "generatedAt" | "lastGoodAt",
): string {
  return capability?.observer[field] ?? UNKNOWN_TEXT;
}

function safeCount(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : UNKNOWN_TEXT;
}

function booleanKey(value: KnownBoolean): MessageKey {
  if (value === true) return "valueYes";
  if (value === false) return "valueNo";
  return "valueUnknown";
}

function operationalTone(operational: Operational): CapabilityTone {
  if (operational === "ready") return "positive";
  if (operational === "starting" || operational === "degraded") {
    return "warning";
  }
  if (operational === "unavailable") return "negative";
  return "neutral";
}

function consequenceKey(
  mode: GatewayMode,
  operational: Operational,
): MessageKey {
  if (operational === "disabled") {
    return mode === "observe" ? "observeModeHint" : "gatewayDisabledHint";
  }
  if (operational === "starting") return "gatewayStartingHint";
  if (operational === "degraded") return "gatewayDegradedHint";
  if (operational === "unavailable") return "gatewayUnavailableHint";
  if (operational === "unknown" || mode === "unknown") {
    return "gatewayUnknownHint";
  }
  if (mode === "observe") return "observeModeHint";
  if (mode === "advise") return "adviseModeHint";
  return "gatewayModeHint";
}

function lastErrorKey(
  capability: RuntimeCapability | null,
  unknownErrorCode: boolean,
): MessageKey | null {
  if (unknownErrorCode) return "gatewayErrorGeneric";
  const code = capability?.gateway.lastError?.code;
  return code ? ERROR_KEY[code] : null;
}

function hasUnknownErrorCode(snapshot: unknown): boolean {
  if (!isRecord(snapshot)) return false;
  const gateway = readOwnData(snapshot, "gateway");
  if (!gateway.ok || !isRecord(gateway.value)) return false;
  const lastError = readOwnData(gateway.value, "lastError");
  if (!lastError.ok || !isRecord(lastError.value)) return false;
  const code = readOwnData(lastError.value, "code");
  if (!code.ok || typeof code.value !== "string") return false;
  return !Object.prototype.hasOwnProperty.call(ERROR_KEY, code.value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  try {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  } catch {
    return false;
  }
}

function readOwnData(
  value: object,
  key: PropertyKey,
): { ok: true; value: unknown } | { ok: false } {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      descriptor === undefined ||
      !Object.prototype.hasOwnProperty.call(descriptor, "value")
    ) {
      return { ok: false };
    }
    return { ok: true, value: descriptor.value };
  } catch {
    return { ok: false };
  }
}
