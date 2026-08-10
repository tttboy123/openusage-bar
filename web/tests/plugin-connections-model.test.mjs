import assert from "node:assert/strict";
import test from "node:test";

import { normalizePluginConnections } from "../.test-dist/pluginConnections.js";

const timestamp = "2026-08-10T03:04:05.123456Z";

export function pluginConnectionsEnvelope() {
  return {
    apiVersion: "plugin.openusage/v1",
    object: "plugin.connections",
    observedAt: timestamp,
    connections: [
      {
        pluginId: "loom",
        configuration: "configured",
        connection: "connected",
        capabilityState: "negotiated",
        capabilities: [
          "decision.lookup",
          "health.query",
          "outcome.record",
          "quotas.query",
          "route.advice",
          "usage.query",
        ],
        lastSeenAt: timestamp,
        lastSyncOutcome: "succeeded",
        lastSyncAt: timestamp,
      },
      {
        pluginId: "codex",
        configuration: "not_configured",
        connection: "never_seen",
        capabilityState: "not_negotiated",
        capabilities: [],
        lastSeenAt: null,
        lastSyncOutcome: "never",
        lastSyncAt: null,
      },
      {
        pluginId: "claude_code",
        configuration: "unknown",
        connection: "unknown",
        capabilityState: "unknown",
        capabilities: [],
        lastSeenAt: null,
        lastSyncOutcome: "unknown",
        lastSyncAt: null,
      },
    ],
  };
}

test("normalizes the exact fixed-order renderer-safe plugin connection projection", () => {
  const input = pluginConnectionsEnvelope();
  const normalized = normalizePluginConnections(input);
  assert.deepEqual(normalized, input);
  assert.notEqual(normalized, input);
  assert.notEqual(normalized?.connections[0], input.connections[0]);
  assert.deepEqual(normalizePluginConnections(normalized), normalized);
});

test("rejects root, order, capability, timestamp, and cross-field drift", () => {
  const mutations = [
    (value) => { value.privateToken = "secret"; },
    (value) => { value.connections[0].endpoint = "http://private.invalid"; },
    (value) => { value.connections.reverse(); },
    (value) => { value.connections[0].capabilities.reverse(); },
    (value) => { value.connections[0].capabilities.push("credentials.read"); },
    (value) => { value.connections[0].capabilities = []; },
    (value) => { value.connections[0].capabilities = ["health.query", "usage.query"]; },
    (value) => { value.connections[1].capabilities = ["health.query"]; },
    (value) => { value.connections[0].configuration = "unknown"; },
    (value) => { value.connections[0].lastSeenAt = null; },
    (value) => { value.connections[0].lastSyncOutcome = "never"; },
    (value) => { value.connections[0].lastSyncAt = null; },
    (value) => {
      value.connections[1].connection = "connected";
      value.connections[1].lastSeenAt = timestamp;
    },
    (value) => { value.connections[1].connection = "unknown"; },
    (value) => { value.connections[1].capabilityState = "unknown"; },
    (value) => { value.connections[1].lastSeenAt = timestamp; },
    (value) => { value.observedAt = "2026-08-10T03:04:05.123Z"; },
    (value) => { value.observedAt = "2026-02-29T03:04:05.123456Z"; },
  ];
  for (const mutate of mutations) {
    const value = pluginConnectionsEnvelope();
    mutate(value);
    assert.equal(normalizePluginConnections(value), null, JSON.stringify(value));
  }
});

test("does not invoke hostile accessors or retain private fields", () => {
  let invoked = false;
  const accessor = pluginConnectionsEnvelope();
  Object.defineProperty(accessor.connections[0], "lastSeenAt", {
    enumerable: true,
    get() {
      invoked = true;
      throw new Error("must not execute");
    },
  });
  assert.doesNotThrow(() => normalizePluginConnections(accessor));
  assert.equal(normalizePluginConnections(accessor), null);
  assert.equal(invoked, false);

  const exotic = pluginConnectionsEnvelope();
  Object.setPrototypeOf(exotic.connections[0], { token: "PRIVATE_CANARY" });
  assert.equal(normalizePluginConnections(exotic), null);

  const proxy = new Proxy(pluginConnectionsEnvelope(), {
    ownKeys() { throw new Error("hostile keys"); },
  });
  assert.doesNotThrow(() => normalizePluginConnections(proxy));
  assert.equal(normalizePluginConnections(proxy), null);

  const hidden = pluginConnectionsEnvelope();
  Object.defineProperty(hidden.connections[0], "credential", {
    enumerable: false,
    value: "PRIVATE_CANARY",
  });
  assert.equal(normalizePluginConnections(hidden), null);
  assert.doesNotMatch(JSON.stringify(normalizePluginConnections(hidden)), /CANARY/u);
});
