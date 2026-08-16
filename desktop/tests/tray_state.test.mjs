import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { capacityProviders } = require("../tray_state.js");

test("capacityProviders unwraps the Local API v1 capacity envelope", () => {
  const providers = [{ providerId: "minimax", remainingRatio: 0.18 }];

  assert.deepEqual(capacityProviders({ providers }), providers);
});

test("capacityProviders keeps the legacy array shape as a compatibility fallback", () => {
  const providers = [{ providerId: "step_plan", remainingRatio: 0.4 }];

  assert.deepEqual(capacityProviders(providers), providers);
});

test("capacityProviders treats unavailable or malformed payloads as empty", () => {
  assert.deepEqual(capacityProviders(null), []);
  assert.deepEqual(capacityProviders({ providers: null }), []);
  assert.deepEqual(capacityProviders({}), []);
});
