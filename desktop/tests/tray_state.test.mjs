import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { balanceRows, capacityProviders } = require("../tray_state.js");

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

test("balanceRows collapses duplicate provider amounts and keeps direct evidence", () => {
  const rows = balanceRows({
    balances: [
      {
        providerId: "deepseek",
        currency: "CNY",
        available: "95.61",
        quality: "derived",
        freshnessSeconds: 10,
        state: "ok",
      },
      {
        providerId: "deepseek",
        currency: "CNY",
        available: "95.61",
        quality: "direct",
        freshnessSeconds: 100,
        state: "ok",
      },
    ],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].quality, "direct");
});
