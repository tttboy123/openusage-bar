import assert from "node:assert/strict";
import test from "node:test";

import { normalizeDecisionTraces } from "../.test-dist/decisionTraces.js";

const API_VERSION = "gateway-decision-trace.openusage/v1";

function routeTrace(overrides = {}) {
  return {
    traceId: "trace_00000000000000000000000000000003",
    occurredAt: "2026-08-10T12:00:03.000000Z",
    kind: "route_advice",
    execution: "advice_only",
    outcome: "defer",
    reason: "quota_low",
    pool: null,
    selected: null,
    exclusions: [],
    fallback: null,
    factsWindow: { durationSeconds: 300 },
    ...overrides,
  };
}

function gatewayTrace(overrides = {}) {
  return {
    traceId: "trace_00000000000000000000000000000002",
    occurredAt: "2026-08-10T12:00:02.000000Z",
    kind: "gateway_execution",
    execution: "executed",
    outcome: "succeeded",
    reason: null,
    pool: null,
    selected: { providerId: "openai", accountDisplayId: null },
    exclusions: [],
    fallback: { attempted: false, attemptCount: 0, finalAction: "none" },
    factsWindow: null,
    ...overrides,
  };
}

function poolTrace(overrides = {}) {
  return {
    traceId: "trace_00000000000000000000000000000001",
    occurredAt: "2026-08-10T12:00:01.000000Z",
    kind: "pool_selection",
    execution: "executed",
    outcome: "selected",
    reason: null,
    pool: { poolId: "primary-us", revision: 7, strategy: "quota-aware" },
    selected: {
      providerId: "anthropic",
      accountDisplayId: "acct_1234567890ab",
    },
    exclusions: [
      { accountDisplayId: "acct_abcdef123456", reason: "cooldown" },
    ],
    fallback: null,
    factsWindow: null,
    ...overrides,
  };
}

function envelope(traces = [routeTrace(), gatewayTrace(), poolTrace()]) {
  return { apiVersion: API_VERSION, traces };
}

test("normalizes the exact newest-first public Decision Trace projection", () => {
  const expected = envelope();
  const normalized = normalizeDecisionTraces(expected);
  assert.deepEqual(normalized, expected);
  assert.notEqual(normalized, expected);
  assert.notEqual(normalized?.traces[0], expected.traces[0]);
  assert.deepEqual(normalizeDecisionTraces(normalized), normalized);
});

test("rejects wrong roots, excess traces, duplicate IDs, and non-newest-first input", () => {
  assert.equal(normalizeDecisionTraces(null), null);
  assert.equal(normalizeDecisionTraces({ ...envelope(), apiVersion: "future/v2" }), null);
  assert.equal(normalizeDecisionTraces({ ...envelope(), extra: true }), null);
  assert.equal(normalizeDecisionTraces({ apiVersion: API_VERSION }), null);
  assert.equal(
    normalizeDecisionTraces(envelope(Array.from({ length: 129 }, (_, index) =>
      routeTrace({
        traceId: `trace_${index.toString(16).padStart(32, "0")}`,
        occurredAt: `2026-08-10T11:59:${String(59 - (index % 60)).padStart(2, "0")}.000000Z`,
      }),
    ))),
    null,
  );
  assert.equal(
    normalizeDecisionTraces(envelope([routeTrace(), routeTrace()])),
    null,
  );
  assert.equal(
    normalizeDecisionTraces(envelope([poolTrace(), routeTrace()])),
    null,
  );
});

test("enforces all three trace-kind invariants and bounded nested contracts", () => {
  const invalid = [
    routeTrace({ execution: "executed" }),
    routeTrace({ outcome: "selected" }),
    routeTrace({ reason: null }),
    routeTrace({ pool: { poolId: "private", revision: 1, strategy: "sticky" } }),
    routeTrace({ selected: { providerId: "openai", accountDisplayId: null } }),
    routeTrace({ exclusions: [{ accountDisplayId: "acct_1234567890ab", reason: "cooldown" }] }),
    routeTrace({ fallback: { attempted: false, attemptCount: 0, finalAction: "none" } }),
    routeTrace({ factsWindow: { durationSeconds: 2_678_401 } }),
    gatewayTrace({ execution: "advice_only" }),
    gatewayTrace({ outcome: "selected" }),
    gatewayTrace({ reason: "quota_low" }),
    gatewayTrace({ selected: { providerId: null, accountDisplayId: null } }),
    gatewayTrace({ selected: { providerId: "openai", accountDisplayId: "acct_1234567890ab" } }),
    gatewayTrace({ fallback: null }),
    gatewayTrace({ fallback: { attempted: true, attemptCount: 1, finalAction: "retry" } }),
    poolTrace({ execution: "advice_only" }),
    poolTrace({ outcome: "succeeded" }),
    poolTrace({ reason: "quota_low" }),
    poolTrace({ pool: { poolId: "private/pool", revision: 1, strategy: "sticky" } }),
    poolTrace({ selected: null }),
    poolTrace({ exclusions: Array.from({ length: 65 }, () => ({
      accountDisplayId: "acct_1234567890ab",
      reason: "quota_unknown",
    })) }),
    poolTrace({ fallback: { attempted: false, attemptCount: 0, finalAction: "none" } }),
  ];
  for (const trace of invalid) {
    assert.equal(normalizeDecisionTraces(envelope([trace])), null, JSON.stringify(trace));
  }

  assert.deepEqual(
    normalizeDecisionTraces(envelope([
      poolTrace({ outcome: "unavailable", selected: null, exclusions: [] }),
    ])),
    envelope([poolTrace({ outcome: "unavailable", selected: null, exclusions: [] })]),
  );
});

test("accepts only canonical valid UTC microsecond timestamps", () => {
  for (const occurredAt of [
    "2026-08-10T12:00:03Z",
    "2026-08-10T12:00:03.000Z",
    "2026-08-10T12:00:03.000000+00:00",
    "2026-02-29T12:00:03.000000Z",
    "0000-01-01T00:00:00.000000Z",
    "2026-08-10T12:00:60.000000Z",
  ]) {
    assert.equal(
      normalizeDecisionTraces(envelope([routeTrace({ occurredAt })])),
      null,
      occurredAt,
    );
  }
  assert.notEqual(
    normalizeDecisionTraces(envelope([
      routeTrace({ occurredAt: "0001-01-01T00:00:00.000000Z" }),
    ])),
    null,
  );
});

test("never invokes hostile accessors or reflects private and future fields", () => {
  const canaries = [
    "PROMPT_CANARY_f0d1",
    "RESPONSE_CANARY_f0d2",
    "CREDENTIAL_CANARY_f0d3",
    "TOKEN_CANARY_f0d4",
    "HEADER_CANARY_f0d5",
    "PATH_CANARY_f0d6",
    "ENDPOINT_CANARY_f0d7",
    "RAW_ERROR_CANARY_f0d8",
    "PRIVATE_ACCOUNT_ID_CANARY_f0d9",
  ];
  for (const field of [
    "prompt",
    "response",
    "credential",
    "token",
    "header",
    "path",
    "endpoint",
    "rawError",
    "accountId",
  ]) {
    const input = envelope();
    input.traces[0][field] = canaries.shift();
    const normalized = normalizeDecisionTraces(input);
    assert.equal(normalized, null);
    assert.doesNotMatch(JSON.stringify(normalized), /CANARY/);
  }

  let invoked = false;
  const hostile = envelope();
  Object.defineProperty(hostile.traces[0], "outcome", {
    enumerable: true,
    get() {
      invoked = true;
      throw new Error("must not run");
    },
  });
  assert.doesNotThrow(() => normalizeDecisionTraces(hostile));
  assert.equal(normalizeDecisionTraces(hostile), null);
  assert.equal(invoked, false);

  const proxy = new Proxy(envelope(), {
    ownKeys() {
      throw new Error("hostile keys");
    },
  });
  assert.doesNotThrow(() => normalizeDecisionTraces(proxy));
  assert.equal(normalizeDecisionTraces(proxy), null);

  const symbolExtra = envelope();
  symbolExtra[Symbol("private")] = "SYMBOL_CANARY_08b2";
  assert.equal(normalizeDecisionTraces(symbolExtra), null);

  const hiddenExtra = envelope();
  Object.defineProperty(hiddenExtra.traces[0], "prompt", {
    value: "HIDDEN_CANARY_08b3",
    enumerable: false,
  });
  assert.equal(normalizeDecisionTraces(hiddenExtra), null);

  const hiddenExpected = envelope();
  Object.defineProperty(hiddenExpected, "apiVersion", {
    value: API_VERSION,
    enumerable: false,
  });
  assert.equal(normalizeDecisionTraces(hiddenExpected), null);
});
