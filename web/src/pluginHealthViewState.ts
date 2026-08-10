export type PluginHealthState =
  | "checking"
  | "initial_unavailable"
  | "refreshing"
  | "last_known"
  | "ready";

export type PluginHealthMessageKey =
  | "pluginConnectionsChecking"
  | "pluginConnectionsRefreshing"
  | "pluginConnectionsUnavailable"
  | "pluginConnectionsLastKnown"
  | "pluginConnectionsUpdated";

export type PluginHealthActionKey =
  | "pluginConnectionsRefresh"
  | "pluginConnectionsRetry";

export interface PluginHealthViewState {
  state: PluginHealthState;
  messageKey: PluginHealthMessageKey | null;
  actionKey: PluginHealthActionKey;
  actionDisabled: boolean;
  actionBusy: boolean;
  showSnapshot: boolean;
  announcementKey: PluginHealthMessageKey;
}

export function pluginHealthViewState(input: unknown): PluginHealthViewState {
  const checking = ownTrue(input, "checking");
  const failed = ownTrue(input, "failed");
  const hasSnapshot = ownTrue(input, "hasSnapshot");

  if (checking) {
    return {
      state: hasSnapshot ? "refreshing" : "checking",
      messageKey: hasSnapshot
        ? "pluginConnectionsRefreshing"
        : "pluginConnectionsChecking",
      actionKey: "pluginConnectionsRefresh",
      actionDisabled: true,
      actionBusy: true,
      showSnapshot: hasSnapshot,
      announcementKey: hasSnapshot
        ? "pluginConnectionsRefreshing"
        : "pluginConnectionsChecking",
    };
  }
  if (failed || !hasSnapshot) {
    return hasSnapshot
      ? {
          state: "last_known",
          messageKey: "pluginConnectionsLastKnown",
          actionKey: "pluginConnectionsRetry",
          actionDisabled: false,
          actionBusy: false,
          showSnapshot: true,
          announcementKey: "pluginConnectionsLastKnown",
        }
      : {
          state: "initial_unavailable",
          messageKey: "pluginConnectionsUnavailable",
          actionKey: "pluginConnectionsRetry",
          actionDisabled: false,
          actionBusy: false,
          showSnapshot: false,
          announcementKey: "pluginConnectionsUnavailable",
        };
  }
  return {
    state: "ready",
    messageKey: null,
    actionKey: "pluginConnectionsRefresh",
    actionDisabled: false,
    actionBusy: false,
    showSnapshot: true,
    announcementKey: "pluginConnectionsUpdated",
  };
}

function ownTrue(input: unknown, key: PropertyKey): boolean {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return false;
  }
  try {
    if (Object.getPrototypeOf(input) !== Object.prototype) return false;
    const descriptor = Object.getOwnPropertyDescriptor(input, key);
    return Boolean(descriptor && "value" in descriptor && descriptor.value === true);
  } catch {
    return false;
  }
}
