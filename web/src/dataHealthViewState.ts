export type DataHealthFilter = "all" | "issues";

export type DataHealthEmptyState =
  | "no_sources"
  | "no_issues"
  | "none"
  | "unknown";

export type DataHealthCauseKey =
  | "healthOk"
  | "healthStale"
  | "healthTemporarily"
  | "healthError"
  | "healthEmptyResult"
  | "healthTimeout"
  | "healthUnauthorized"
  | "healthUnknown";

export type DataHealthStateKind = "ok" | "warn" | "bad";

export type DataHealthStateLabelKey =
  | "healthStateOk"
  | "healthStateStale"
  | "healthStateUnavailable"
  | "healthStateError"
  | "healthStateUnknown";

export type DataHealthStatePresentation = Readonly<{
  kind: DataHealthStateKind;
  labelKey: DataHealthStateLabelKey;
}>;

type SourceFact = "state" | "errorCode" | "providerId" | "sourceId";

const MAX_SOURCE_FACT_LENGTH = 256;

const STATE_PRESENTATIONS = Object.freeze({
  ok: Object.freeze({ kind: "ok", labelKey: "healthStateOk" }),
  stale: Object.freeze({ kind: "warn", labelKey: "healthStateStale" }),
  unavailable: Object.freeze({
    kind: "warn",
    labelKey: "healthStateUnavailable",
  }),
  error: Object.freeze({ kind: "bad", labelKey: "healthStateError" }),
  unknown: Object.freeze({ kind: "warn", labelKey: "healthStateUnknown" }),
} satisfies Record<string, DataHealthStatePresentation>);

function sourceFact(item: unknown, field: SourceFact): string {
  if (typeof item !== "object" || item === null) return "";

  try {
    const descriptor = Object.getOwnPropertyDescriptor(item, field);
    if (!descriptor || !("value" in descriptor)) return "";
    return typeof descriptor.value === "string"
      ? descriptor.value.slice(0, MAX_SOURCE_FACT_LENGTH)
      : "";
  } catch {
    return "";
  }
}

function normalizedSourceFact(item: unknown, field: SourceFact): string {
  return sourceFact(item, field).toLowerCase();
}

export function dataHealthCauseKey(item: unknown): DataHealthCauseKey {
  const state = normalizedSourceFact(item, "state");
  const errorCode = normalizedSourceFact(item, "errorCode");

  if (state === "ok" || state === "available") return "healthOk";
  if (errorCode === "empty_result") return "healthEmptyResult";
  if (errorCode === "timeout") return "healthTimeout";
  if (
    errorCode === "unauthorized" ||
    errorCode === "invalid_credentials" ||
    errorCode === "auth_expired" ||
    errorCode === "401"
  ) {
    return "healthUnauthorized";
  }
  if (state === "stale") return "healthStale";
  if (state === "temporarily_unavailable") return "healthTemporarily";
  if (state === "error") return "healthError";
  return "healthUnknown";
}

export function dataHealthStatePresentation(
  item: unknown,
): DataHealthStatePresentation {
  const state = normalizedSourceFact(item, "state");
  if (state === "ok" || state === "available") return STATE_PRESENTATIONS.ok;
  if (state === "stale") return STATE_PRESENTATIONS.stale;
  if (state === "temporarily_unavailable") {
    return STATE_PRESENTATIONS.unavailable;
  }
  if (state === "error") return STATE_PRESENTATIONS.error;
  return STATE_PRESENTATIONS.unknown;
}

function hashSourceIdentity(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36).padStart(7, "0");
}

export function dataHealthSourceKey(
  item: unknown,
  snapshotIndex: number,
): string {
  const index = isCount(snapshotIndex) ? snapshotIndex : 0;
  const providerId = sourceFact(item, "providerId");
  const sourceId = sourceFact(item, "sourceId");
  const digest = hashSourceIdentity(`${index}\u0000${providerId}\u0000${sourceId}`);
  return `source-${index.toString(36)}-${digest}`;
}

function isCount(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
}

export function dataHealthEmptyState(
  totalSources: unknown,
  issueCount: unknown,
  filter: unknown,
): DataHealthEmptyState {
  if (
    !isCount(totalSources) ||
    !isCount(issueCount) ||
    issueCount > totalSources ||
    (filter !== "all" && filter !== "issues")
  ) {
    return "unknown";
  }

  if (totalSources === 0) return "no_sources";
  if (filter === "issues" && issueCount === 0) return "no_issues";
  return "none";
}
