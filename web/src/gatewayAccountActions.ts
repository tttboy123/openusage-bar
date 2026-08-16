export const GATEWAY_ACCOUNT_API_VERSION =
  "gateway-account-host.openusage/v1" as const;

export const GATEWAY_PROVIDER_PRESETS = [
  "openai",
  "anthropic",
  "deepseek",
  "openrouter",
] as const;

export type GatewayProviderPreset = (typeof GATEWAY_PROVIDER_PRESETS)[number];

export const GATEWAY_ACCOUNT_ACTIONS = [
  "gatewayAccount.openCreate",
  "gatewayAccount.openEdit",
  "gatewayAccount.openReplace",
  "gatewayAccount.openRemove",
] as const;

export type GatewayAccountAction =
  (typeof GATEWAY_ACCOUNT_ACTIONS)[number];

export interface GatewayAccountCapability {
  apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
  actions: GatewayAccountAction[];
}

export type GatewayAccountIntent =
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      action: "gatewayAccount.openCreate";
      preset: GatewayProviderPreset;
    }
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      action:
        | "gatewayAccount.openEdit"
        | "gatewayAccount.openReplace"
        | "gatewayAccount.openRemove";
      displayId: string;
    };

export type GatewayAccountOperationFailureCode =
  | "invalid_intent"
  | "not_found"
  | "already_exists"
  | "invalid_input"
  | "credential_unavailable"
  | "config_write_failed"
  | "account_in_use"
  | "helper_busy"
  | "service_unavailable";

export type GatewayAccountLaunchErrorCode =
  | "helper_busy"
  | "service_unavailable"
  | "invalid_intent";

export interface GatewayAccountLaunchError {
  error: { code: GatewayAccountLaunchErrorCode };
}

export interface GatewayAccountOperationResponse {
  apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
  operationId: string;
  state: "opened";
}

export type GatewayAccountOperationStatus =
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      operationId: string;
      state: "pending";
    }
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      operationId: string;
      state: "succeeded";
      code: "ok";
    }
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      operationId: string;
      state: "cancelled";
      code: "cancelled";
    }
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      operationId: string;
      state: "timed_out";
      code: "timed_out";
    }
  | {
      apiVersion: typeof GATEWAY_ACCOUNT_API_VERSION;
      operationId: string;
      state: "failed";
      code: GatewayAccountOperationFailureCode;
    };

export interface GatewayAccountHostAdapter {
  readCapability(signal?: AbortSignal): Promise<unknown>;
  launch(intent: GatewayAccountIntent, signal?: AbortSignal): Promise<unknown>;
  readOperation(operationId: string, signal?: AbortSignal): Promise<unknown>;
}

type JsonRecord = Record<PropertyKey, unknown>;

const OPERATION_ID = /^op_[0-9a-f]{32}$/u;
const OPERATION_FAILURE_CODES = new Set<GatewayAccountOperationFailureCode>([
  "invalid_intent",
  "not_found",
  "already_exists",
  "invalid_input",
  "credential_unavailable",
  "config_write_failed",
  "account_in_use",
  "helper_busy",
  "service_unavailable",
]);

const GATEWAY_ACCOUNT_CAPABILITY_PATH = "/host/v2/gateway-account-capabilities";
const GATEWAY_ACCOUNT_OPERATIONS_PATH = "/host/v2/gateway-account-operations";

export function createBrowserGatewayAccountHostAdapter(
  fetcher: typeof fetch = fetch,
): GatewayAccountHostAdapter {
  const common = {
    credentials: "omit" as const,
    cache: "no-store" as const,
    referrerPolicy: "no-referrer" as const,
  };
  return {
    async readCapability(signal) {
      const response = await fetcher(GATEWAY_ACCOUNT_CAPABILITY_PATH, {
        ...common,
        method: "GET",
        signal,
      });
      return response.ok ? response.json() : null;
    },
    async launch(intent, signal) {
      const response = await fetcher(GATEWAY_ACCOUNT_OPERATIONS_PATH, {
        ...common,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(intent),
        signal,
      });
      if (response.ok) return response.json();
      try {
        const error = normalizeGatewayAccountLaunchError(await response.json());
        return error ?? { error: { code: "service_unavailable" } };
      } catch {
        return { error: { code: "service_unavailable" } };
      }
    },
    async readOperation(operationId, signal) {
      if (!OPERATION_ID.test(operationId)) return null;
      const response = await fetcher(
        `${GATEWAY_ACCOUNT_OPERATIONS_PATH}/${operationId}`,
        {
          ...common,
          method: "GET",
          signal,
        },
      );
      return response.ok ? response.json() : null;
    },
  };
}

export function normalizeGatewayAccountLaunchError(
  value: unknown,
): GatewayAccountLaunchError | null {
  try {
    if (!isPlainRecord(value) || !exactOwnDataKeys(value, ["error"])) return null;
    const error = readDataValue(value, "error");
    if (!isPlainRecord(error) || !exactOwnDataKeys(error, ["code"])) return null;
    const code = readDataValue(error, "code");
    if (
      code !== "helper_busy" &&
      code !== "service_unavailable" &&
      code !== "invalid_intent"
    ) {
      return null;
    }
    return { error: { code } };
  } catch {
    return null;
  }
}

export function normalizeGatewayAccountOperationResponse(
  value: unknown,
): GatewayAccountOperationResponse | null {
  try {
    if (
      !isPlainRecord(value) ||
      !exactOwnDataKeys(value, ["apiVersion", "operationId", "state"]) ||
      readDataValue(value, "apiVersion") !== GATEWAY_ACCOUNT_API_VERSION ||
      readDataValue(value, "state") !== "opened"
    ) {
      return null;
    }
    const operationId = readDataValue(value, "operationId");
    return typeof operationId === "string" && OPERATION_ID.test(operationId)
      ? {
          apiVersion: GATEWAY_ACCOUNT_API_VERSION,
          operationId,
          state: "opened",
        }
      : null;
  } catch {
    return null;
  }
}

export function normalizeGatewayAccountOperationStatus(
  value: unknown,
): GatewayAccountOperationStatus | null {
  try {
    if (!isPlainRecord(value)) return null;
    const state = readDataValue(value, "state");
    const expectedKeys = state === "pending"
      ? ["apiVersion", "operationId", "state"]
      : ["apiVersion", "operationId", "state", "code"];
    if (
      !exactOwnDataKeys(value, expectedKeys) ||
      readDataValue(value, "apiVersion") !== GATEWAY_ACCOUNT_API_VERSION
    ) {
      return null;
    }
    const operationId = readDataValue(value, "operationId");
    if (typeof operationId !== "string" || !OPERATION_ID.test(operationId)) {
      return null;
    }
    if (state === "pending") {
      return { apiVersion: GATEWAY_ACCOUNT_API_VERSION, operationId, state };
    }
    const code = readDataValue(value, "code");
    if (state === "succeeded" && code === "ok") {
      return { apiVersion: GATEWAY_ACCOUNT_API_VERSION, operationId, state, code };
    }
    if (state === "cancelled" && code === "cancelled") {
      return { apiVersion: GATEWAY_ACCOUNT_API_VERSION, operationId, state, code };
    }
    if (state === "timed_out" && code === "timed_out") {
      return { apiVersion: GATEWAY_ACCOUNT_API_VERSION, operationId, state, code };
    }
    if (
      state === "failed" &&
      typeof code === "string" &&
      OPERATION_FAILURE_CODES.has(code as GatewayAccountOperationFailureCode)
    ) {
      return {
        apiVersion: GATEWAY_ACCOUNT_API_VERSION,
        operationId,
        state,
        code: code as GatewayAccountOperationFailureCode,
      };
    }
    return null;
  } catch {
    return null;
  }
}

export function normalizeGatewayAccountCapability(
  value: unknown,
): GatewayAccountCapability | null {
  try {
    if (
      !isPlainRecord(value) ||
      !exactOwnDataKeys(value, ["apiVersion", "actions"]) ||
      readDataValue(value, "apiVersion") !== GATEWAY_ACCOUNT_API_VERSION
    ) {
      return null;
    }
    const actions = exactDataArray(
      readDataValue(value, "actions"),
      GATEWAY_ACCOUNT_ACTIONS.length,
    );
    if (actions === null) return null;
    const actionSet = new Set(actions);
    if (
      actionSet.size !== GATEWAY_ACCOUNT_ACTIONS.length ||
      GATEWAY_ACCOUNT_ACTIONS.some((action) => !actionSet.has(action))
    ) {
      return null;
    }
    return {
      apiVersion: GATEWAY_ACCOUNT_API_VERSION,
      actions: [...GATEWAY_ACCOUNT_ACTIONS],
    };
  } catch {
    return null;
  }
}

export function buildGatewayAccountIntent(
  action: unknown,
  target: unknown,
): GatewayAccountIntent | null {
  try {
    if (!isPlainRecord(target)) return null;
    if (action === "gatewayAccount.openCreate") {
      if (!exactOwnDataKeys(target, ["preset"])) return null;
      const preset = readDataValue(target, "preset");
      if (
        typeof preset !== "string" ||
        !GATEWAY_PROVIDER_PRESETS.includes(preset as GatewayProviderPreset)
      ) {
        return null;
      }
      return {
        apiVersion: GATEWAY_ACCOUNT_API_VERSION,
        action,
        preset: preset as GatewayProviderPreset,
      };
    }
    if (
      action !== "gatewayAccount.openEdit" &&
      action !== "gatewayAccount.openReplace" &&
      action !== "gatewayAccount.openRemove"
    ) {
      return null;
    }
    if (!exactOwnDataKeys(target, ["displayId"])) return null;
    const displayId = readDataValue(target, "displayId");
    if (typeof displayId !== "string" || !/^acct_[0-9a-f]{12}$/u.test(displayId)) {
      return null;
    }
    return {
      apiVersion: GATEWAY_ACCOUNT_API_VERSION,
      action,
      displayId,
    };
  } catch {
    return null;
  }
}

function isPlainRecord(value: unknown): value is JsonRecord {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function exactOwnDataKeys(value: JsonRecord, expected: readonly string[]): boolean {
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const names = Object.getOwnPropertyNames(descriptors);
  return (
    Object.getOwnPropertySymbols(value).length === 0 &&
    names.length === expected.length &&
    expected.every((key) => {
      const descriptor = descriptors[key];
      return descriptor !== undefined && "value" in descriptor;
    })
  );
}

function exactDataArray(value: unknown, length: number): unknown[] | null {
  if (
    !Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Array.prototype ||
    value.length !== length
  ) {
    return null;
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  if (Reflect.ownKeys(descriptors).length !== length + 1) return null;
  const result: unknown[] = [];
  for (let index = 0; index < length; index += 1) {
    const descriptor = descriptors[String(index)];
    if (!descriptor || !("value" in descriptor) || descriptor.enumerable !== true) {
      return null;
    }
    result.push(descriptor.value);
  }
  return result;
}

function readDataValue(value: JsonRecord, key: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
}
