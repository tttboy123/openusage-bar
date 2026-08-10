import assert from "node:assert/strict";
import test from "node:test";

import {
  buildGatewayAccountIntent,
  createBrowserGatewayAccountHostAdapter,
  normalizeGatewayAccountCapability,
  normalizeGatewayAccountLaunchError,
  normalizeGatewayAccountOperationResponse,
  normalizeGatewayAccountOperationStatus,
} from "../.test-dist/gatewayAccountActions.js";

test("normalizes only exact public launch errors", () => {
  for (const code of ["helper_busy", "service_unavailable", "invalid_intent"]) {
    assert.deepEqual(normalizeGatewayAccountLaunchError({ error: { code } }), {
      error: { code },
    });
  }
  for (const invalid of [
    { error: { code: "credential_unavailable" } },
    { error: { code: "helper_busy", detail: "/private/path" } },
    { error: { code: "invalid_intent" }, operationId: "op_private" },
    { code: "helper_busy" },
  ]) {
    assert.equal(normalizeGatewayAccountLaunchError(invalid), null);
  }
});

test("builds an exact public Gateway account create intent from an allowlisted preset", () => {
  assert.deepEqual(
    buildGatewayAccountIntent(
      "gatewayAccount.openCreate",
      { preset: "openai" },
    ),
    {
      apiVersion: "gateway-account-host.openusage/v1",
      action: "gatewayAccount.openCreate",
      preset: "openai",
    },
  );

  for (const invalid of [
    { preset: "minimax" },
    { preset: "codex" },
    { preset: "openai", credential: "CANARY" },
    { preset: "openai", providerId: "private" },
  ]) {
    assert.equal(
      buildGatewayAccountIntent("gatewayAccount.openCreate", invalid),
      null,
    );
  }
});

test("builds only displayId intents for edit, credential replacement, and remove", () => {
  for (const action of [
    "gatewayAccount.openEdit",
    "gatewayAccount.openReplace",
    "gatewayAccount.openRemove",
  ]) {
    assert.deepEqual(
      buildGatewayAccountIntent(action, { displayId: "acct_0123456789ab" }),
      {
        apiVersion: "gateway-account-host.openusage/v1",
        action,
        displayId: "acct_0123456789ab",
      },
    );
  }

  const privateFields = [
    "credential",
    "token",
    "header",
    "accountId",
    "providerId",
    "endpoint",
    "path",
    "env",
    "command",
  ];
  for (const field of privateFields) {
    assert.equal(
      buildGatewayAccountIntent("gatewayAccount.openEdit", {
        displayId: "acct_0123456789ab",
        [field]: "CANARY_PRIVATE",
      }),
      null,
    );
  }

  let invoked = 0;
  const hostile = {};
  Object.defineProperty(hostile, "displayId", {
    enumerable: true,
    get() {
      invoked += 1;
      return "acct_0123456789ab";
    },
  });
  assert.equal(
    buildGatewayAccountIntent("gatewayAccount.openRemove", hostile),
    null,
  );
  assert.equal(invoked, 0);
});

test("trusts only the exact Gateway ProviderAccountRef host capability", () => {
  const exact = {
    apiVersion: "gateway-account-host.openusage/v1",
    actions: [
      "gatewayAccount.openCreate",
      "gatewayAccount.openEdit",
      "gatewayAccount.openReplace",
      "gatewayAccount.openRemove",
    ],
  };
  assert.deepEqual(normalizeGatewayAccountCapability(exact), exact);

  for (const invalid of [
    { ...exact, actions: exact.actions.slice(0, 3) },
    { ...exact, actions: [...exact.actions, "gatewayAccount.readSecret"] },
    { ...exact, credential: "CANARY" },
    { ...exact, apiVersion: "gateway-account-host.openusage/v2" },
  ]) {
    assert.equal(normalizeGatewayAccountCapability(invalid), null);
  }
});

test("keeps only opaque public operation responses and exact terminal status codes", () => {
  const operationId = "op_0123456789abcdef0123456789abcdef";
  assert.deepEqual(
    normalizeGatewayAccountOperationResponse({
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "opened",
    }),
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "opened",
    },
  );
  assert.deepEqual(
    normalizeGatewayAccountOperationStatus({
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "pending",
    }),
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "pending",
    },
  );
  for (const [state, code] of [
    ["succeeded", "ok"],
    ["cancelled", "cancelled"],
    ["timed_out", "timed_out"],
    ["failed", "credential_unavailable"],
    ["failed", "account_in_use"],
  ]) {
    assert.deepEqual(
      normalizeGatewayAccountOperationStatus({
        apiVersion: "gateway-account-host.openusage/v1",
        operationId,
        state,
        code,
      }),
      {
        apiVersion: "gateway-account-host.openusage/v1",
        operationId,
        state,
        code,
      },
    );
  }

  for (const invalid of [
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId: "acct_0123456789ab",
      state: "opened",
    },
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "pending",
      code: "ok",
    },
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "succeeded",
      code: "credential_unavailable",
    },
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "failed",
      code: "private path /tmp/config",
    },
    {
      apiVersion: "gateway-account-host.openusage/v1",
      operationId,
      state: "cancelled",
      code: "cancelled",
      accountId: "private",
    },
  ]) {
    assert.equal(normalizeGatewayAccountOperationResponse(invalid), null);
    assert.equal(normalizeGatewayAccountOperationStatus(invalid), null);
  }
});

test("uses only the versioned credential-free ProviderAccountRef host endpoints", async () => {
  const calls = [];
  const fetcher = async (path, options) => {
    calls.push({ path, options });
    return { ok: true, async json() { return { accepted: true }; } };
  };
  const adapter = createBrowserGatewayAccountHostAdapter(fetcher);
  const intent = buildGatewayAccountIntent(
    "gatewayAccount.openCreate",
    { preset: "anthropic" },
  );
  assert.ok(intent);
  await adapter.readCapability();
  await adapter.launch(intent);
  await adapter.readOperation("op_0123456789abcdef0123456789abcdef");

  assert.deepEqual(calls.map(({ path, options }) => ({
    path,
    method: options.method,
    credentials: options.credentials,
    cache: options.cache,
    referrerPolicy: options.referrerPolicy,
  })), [
    {
      path: "/host/v2/gateway-account-capabilities",
      method: "GET",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    },
    {
      path: "/host/v2/gateway-account-operations",
      method: "POST",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    },
    {
      path: "/host/v2/gateway-account-operations/op_0123456789abcdef0123456789abcdef",
      method: "GET",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    },
  ]);
  assert.deepEqual(JSON.parse(calls[1].options.body), intent);
  assert.deepEqual(calls[1].options.headers, { "Content-Type": "application/json" });
});

test("POST non-2xx exposes only exact allowlisted safe launch errors", async () => {
  for (const [body, expected] of [
    [{ error: { code: "helper_busy" } }, { error: { code: "helper_busy" } }],
    [{ error: { code: "invalid_intent" } }, { error: { code: "invalid_intent" } }],
    [{ error: { code: "helper_busy", detail: "private" } }, { error: { code: "service_unavailable" } }],
  ]) {
    const adapter = createBrowserGatewayAccountHostAdapter(async () => ({
      ok: false,
      async json() { return body; },
    }));
    const intent = buildGatewayAccountIntent("gatewayAccount.openCreate", { preset: "openai" });
    assert.deepEqual(await adapter.launch(intent), expected);
  }
});
