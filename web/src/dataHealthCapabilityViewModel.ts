import type { Messages } from "./i18n";
import {
  normalizeRuntimeCapability,
  type GatewayMode,
  type Operational,
} from "./runtimeCapability";

type MessageKey = keyof Messages;
type CapabilityTone = "positive" | "warning" | "negative" | "neutral";

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

export interface DataHealthCapabilityViewModel {
  observer: {
    titleKey: MessageKey;
    operationalKey: MessageKey;
    tone: CapabilityTone;
  };
  gateway: {
    titleKey: MessageKey;
    optionalKey: MessageKey;
    modeKey: MessageKey;
    overallOperationalKey: MessageKey;
    tone: CapabilityTone;
    consequenceKey: MessageKey;
  };
  providerHealth: {
    titleKey: MessageKey;
    summaryKey: MessageKey;
    summaryValues: Record<string, string>;
    tone: CapabilityTone;
    isIssue: boolean;
    configuredCountText: string;
    healthyCountText: string;
  };
}

const UNKNOWN_TEXT = "—";

export function dataHealthCapabilityViewModel(
  snapshot: unknown,
): DataHealthCapabilityViewModel {
  const capability = normalizeRuntimeCapability(snapshot);
  const mode = capability?.gateway.mode ?? "unknown";
  const observerOperational = capability?.observer.operational ?? "unknown";
  const gatewayOperational = capability?.gateway.operational ?? "unknown";
  const configured = capability?.gateway.configuredProviderCount ?? null;
  const healthy = capability?.gateway.healthyProviderCount ?? null;

  return {
    observer: {
      titleKey: "observerTitle",
      operationalKey: OPERATIONAL_KEY[observerOperational],
      tone: operationalTone(observerOperational),
    },
    gateway: {
      titleKey: "gatewayTitle",
      optionalKey: "gatewayOptional",
      modeKey: MODE_KEY[mode],
      overallOperationalKey: OPERATIONAL_KEY[gatewayOperational],
      tone: operationalTone(gatewayOperational),
      consequenceKey: consequenceKey(mode, gatewayOperational),
    },
    providerHealth: providerHealthViewModel(mode, configured, healthy),
  };
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

function providerHealthViewModel(
  mode: "observe" | "advise" | "gateway" | "unknown",
  configured: number | null,
  healthy: number | null,
): DataHealthCapabilityViewModel["providerHealth"] {
  if (mode === "observe" || mode === "advise") {
    return providerHealthResult("notActiveInMode");
  }
  if (mode !== "gateway" || configured === null) {
    return providerHealthResult("providerHealthUnknown", configured, healthy);
  }
  if (configured === 0) {
    return providerHealthResult("noGatewayProviders", configured, healthy);
  }
  if (healthy === null) {
    return providerHealthResult("providerHealthUnknown", configured, healthy);
  }

  const tone: CapabilityTone = healthy === configured
    ? "positive"
    : healthy === 0
      ? "negative"
      : "warning";
  return providerHealthResult(
    "providerCount",
    configured,
    healthy,
    tone,
    healthy < configured,
  );
}

function providerHealthResult(
  summaryKey: MessageKey,
  configured: number | null = null,
  healthy: number | null = null,
  tone: CapabilityTone = "neutral",
  isIssue = false,
): DataHealthCapabilityViewModel["providerHealth"] {
  const configuredCountText = countText(configured);
  const healthyCountText = countText(healthy);
  return {
    titleKey: "gatewayProviders",
    summaryKey,
    summaryValues: {
      configured: configuredCountText,
      healthy: healthyCountText,
    },
    tone,
    isIssue,
    configuredCountText,
    healthyCountText,
  };
}

function countText(value: number | null): string {
  return value === null ? UNKNOWN_TEXT : String(value);
}
