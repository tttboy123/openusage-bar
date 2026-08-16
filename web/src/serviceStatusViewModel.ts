export type ServiceStatusState =
  | "checking"
  | "initial_failure"
  | "refresh_failure"
  | "ready";

export type ServiceStatusMessageKey =
  | "checkingServiceStatus"
  | "serviceStatusUnavailable"
  | "serviceStatusLastKnown"
  | "serviceStatusUpdated";

export type ServiceStatusActionKey =
  | "refreshServiceStatus"
  | "retryServiceStatus";

export interface ServiceStatusViewModel {
  state: ServiceStatusState;
  messageKey: ServiceStatusMessageKey | null;
  actionKey: ServiceStatusActionKey;
  actionDisabled: boolean;
  actionBusy: boolean;
  showSnapshot: boolean;
  announcementKey: ServiceStatusMessageKey;
}

function ownTrue(input: unknown, key: PropertyKey): boolean {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return false;
  }
  try {
    const descriptor = Object.getOwnPropertyDescriptor(input, key);
    return Boolean(
      descriptor &&
        Object.prototype.hasOwnProperty.call(descriptor, "value") &&
        descriptor.value === true,
    );
  } catch {
    return false;
  }
}

export function serviceStatusViewModel(input: unknown): ServiceStatusViewModel {
  const checking = ownTrue(input, "checking");
  const failed = ownTrue(input, "failed");
  const hasSnapshot = ownTrue(input, "hasSnapshot");

  if (checking) {
    return {
      state: "checking",
      messageKey: "checkingServiceStatus",
      actionKey: "refreshServiceStatus",
      actionDisabled: true,
      actionBusy: true,
      showSnapshot: hasSnapshot,
      announcementKey: "checkingServiceStatus",
    };
  }

  if (failed) {
    return hasSnapshot
      ? {
          state: "refresh_failure",
          messageKey: "serviceStatusLastKnown",
          actionKey: "retryServiceStatus",
          actionDisabled: false,
          actionBusy: false,
          showSnapshot: true,
          announcementKey: "serviceStatusLastKnown",
        }
      : {
          state: "initial_failure",
          messageKey: "serviceStatusUnavailable",
          actionKey: "retryServiceStatus",
          actionDisabled: false,
          actionBusy: false,
          showSnapshot: false,
          announcementKey: "serviceStatusUnavailable",
        };
  }

  return {
    state: "ready",
    messageKey: null,
    actionKey: "refreshServiceStatus",
    actionDisabled: false,
    actionBusy: false,
    showSnapshot: hasSnapshot,
    announcementKey: "serviceStatusUpdated",
  };
}
