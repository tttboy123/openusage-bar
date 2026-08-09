export type ObserverOperatingSystem = "macos" | "windows" | "linux" | null;
export type ObserverPlatformSupport = "supported" | "unsupported" | "unknown";
export type ObserverPlatformReasonCode =
  | "supported_sources_available"
  | "source_level_evidence_unverified"
  | "runtime_platform_unknown";

export interface ObserverPlatformCapability {
  operatingSystem: ObserverOperatingSystem;
  support: ObserverPlatformSupport;
  supportedSourceCount: number | null;
  totalSourceCount: number;
  reasonCode: ObserverPlatformReasonCode;
}

export type ObserverPlatformTone = "positive" | "neutral" | "unknown";
export type ObserverPlatformStateKey =
  | "observerPlatformSupported"
  | "observerPlatformUnverified"
  | "observerPlatformUnknown";
export type ObserverPlatformDetailKey =
  | "observerPlatformVerifiedCount"
  | "observerPlatformCountUnknown";
export type ObserverOperatingSystemKey =
  | "platformMacOS"
  | "platformWindows"
  | "platformLinux"
  | "platformUnknown";

export interface ObserverPlatformViewModel {
  tone: ObserverPlatformTone;
  stateKey: ObserverPlatformStateKey;
  detailKey: ObserverPlatformDetailKey;
  supportedSourceCount: number | null;
  totalSourceCount: number | null;
  operatingSystemKey: ObserverOperatingSystemKey;
}

type JsonRecord = Record<PropertyKey, unknown>;

const ALLOWED_FIELDS = new Set([
  "operatingSystem",
  "support",
  "supportedSourceCount",
  "totalSourceCount",
  "reasonCode",
]);

const OPERATING_SYSTEM_KEY: Record<
  Exclude<ObserverOperatingSystem, null>,
  ObserverOperatingSystemKey
> = {
  macos: "platformMacOS",
  windows: "platformWindows",
  linux: "platformLinux",
};

/**
 * Rebuild the renderer-safe projection from an untrusted value. The accepted
 * states are deliberately closed so verified zero can never collapse into an
 * unknown runtime, and additive/private fields never cross the UI boundary.
 */
export function normalizeObserverPlatformCapability(
  value: unknown,
): ObserverPlatformCapability | null {
  try {
    if (!isRecord(value) || !hasOnlyAllowedFields(value)) return null;

    const operatingSystem = ownData(value, "operatingSystem");
    const support = ownData(value, "support");
    const supportedSourceCount = ownData(value, "supportedSourceCount");
    const totalSourceCount = ownData(value, "totalSourceCount");
    const reasonCode = ownData(value, "reasonCode");
    if (
      !operatingSystem.ok ||
      !support.ok ||
      !supportedSourceCount.ok ||
      !totalSourceCount.ok ||
      !reasonCode.ok ||
      !isPositiveCount(totalSourceCount.value)
    ) {
      return null;
    }

    if (
      operatingSystem.value === null &&
      support.value === "unknown" &&
      supportedSourceCount.value === null &&
      reasonCode.value === "runtime_platform_unknown"
    ) {
      return {
        operatingSystem: null,
        support: "unknown",
        supportedSourceCount: null,
        totalSourceCount: totalSourceCount.value,
        reasonCode: "runtime_platform_unknown",
      };
    }

    if (!isKnownOperatingSystem(operatingSystem.value)) return null;

    if (
      support.value === "unsupported" &&
      supportedSourceCount.value === 0 &&
      reasonCode.value === "source_level_evidence_unverified"
    ) {
      return {
        operatingSystem: operatingSystem.value,
        support: "unsupported",
        supportedSourceCount: 0,
        totalSourceCount: totalSourceCount.value,
        reasonCode: "source_level_evidence_unverified",
      };
    }

    if (
      support.value === "supported" &&
      isPositiveCount(supportedSourceCount.value) &&
      supportedSourceCount.value <= totalSourceCount.value &&
      reasonCode.value === "supported_sources_available"
    ) {
      return {
        operatingSystem: operatingSystem.value,
        support: "supported",
        supportedSourceCount: supportedSourceCount.value,
        totalSourceCount: totalSourceCount.value,
        reasonCode: "supported_sources_available",
      };
    }

    return null;
  } catch {
    return null;
  }
}

export function observerPlatformViewModel(
  value: unknown,
): ObserverPlatformViewModel {
  const capability = normalizeObserverPlatformCapability(value);
  if (capability === null) return unknownViewModel();

  if (capability.support === "unknown") {
    return {
      ...unknownViewModel(),
      totalSourceCount: capability.totalSourceCount,
    };
  }
  if (capability.operatingSystem === null) return unknownViewModel();

  return {
    tone: capability.support === "supported" ? "positive" : "neutral",
    stateKey: capability.support === "supported"
      ? "observerPlatformSupported"
      : "observerPlatformUnverified",
    detailKey: "observerPlatformVerifiedCount",
    supportedSourceCount: capability.supportedSourceCount,
    totalSourceCount: capability.totalSourceCount,
    operatingSystemKey: OPERATING_SYSTEM_KEY[capability.operatingSystem],
  };
}

function unknownViewModel(): ObserverPlatformViewModel {
  return {
    tone: "unknown",
    stateKey: "observerPlatformUnknown",
    detailKey: "observerPlatformCountUnknown",
    supportedSourceCount: null,
    totalSourceCount: null,
    operatingSystemKey: "platformUnknown",
  };
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyAllowedFields(value: JsonRecord): boolean {
  const names = Object.getOwnPropertyNames(value);
  return (
    names.length === ALLOWED_FIELDS.size &&
    names.every((name) => ALLOWED_FIELDS.has(name)) &&
    Object.getOwnPropertySymbols(value).length === 0
  );
}

function ownData(
  value: JsonRecord,
  key: string,
): { ok: true; value: unknown } | { ok: false } {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  if (!descriptor || !("value" in descriptor)) return { ok: false };
  return { ok: true, value: descriptor.value };
}

function isKnownOperatingSystem(
  value: unknown,
): value is Exclude<ObserverOperatingSystem, null> {
  return value === "macos" || value === "windows" || value === "linux";
}

function isPositiveCount(value: unknown): value is number {
  return Number.isSafeInteger(value) && typeof value === "number" && value > 0;
}
