import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPoolHostAction,
  normalizePoolHostActionResult,
  normalizeTrustedHostCapability,
} from "../.test-dist/accountPoolActions.js";

const TRUSTED_CAPABILITY = {
  apiVersion: "host-action.openusage/v1",
  actions: [
    "accountPool.create",
    "accountPool.edit",
    "accountPool.remove",
  ],
};

test("enables Pool management only for the exact trusted-host capability", () => {
  assert.deepEqual(normalizeTrustedHostCapability(TRUSTED_CAPABILITY), TRUSTED_CAPABILITY);

  const hostileAccessor = structuredClone(TRUSTED_CAPABILITY);
  Object.defineProperty(hostileAccessor, "actions", {
    enumerable: true,
    get() {
      throw new Error("must not invoke accessors");
    },
  });
  for (const candidate of [
    null,
    { ...TRUSTED_CAPABILITY, apiVersion: "future" },
    { ...TRUSTED_CAPABILITY, actions: ["accountPool.create"] },
    {
      ...TRUSTED_CAPABILITY,
      actions: [...TRUSTED_CAPABILITY.actions, "accountPool.remove"],
    },
    { ...TRUSTED_CAPABILITY, credential: "CANARY_CREDENTIAL" },
    hostileAccessor,
  ]) {
    assert.equal(normalizeTrustedHostCapability(candidate), null);
  }
});

test("trusted-host capability never invokes hostile array accessors", () => {
  let invoked = 0;
  const actions = [...TRUSTED_CAPABILITY.actions];
  Object.defineProperty(actions, "1", {
    enumerable: true,
    get() {
      invoked += 1;
      return "accountPool.edit";
    },
  });
  assert.equal(
    normalizeTrustedHostCapability({
      apiVersion: "host-action.openusage/v1",
      actions,
    }),
    null,
  );
  assert.equal(invoked, 0);
});

test("rejects host-action records with inherited private state", () => {
  const capability = Object.create({ credential: "CANARY_CREDENTIAL" });
  capability.apiVersion = "host-action.openusage/v1";
  capability.actions = [...TRUSTED_CAPABILITY.actions];

  assert.equal(normalizeTrustedHostCapability(capability), null);
});

test("normalizes only exact sanitized host-action results", () => {
  assert.deepEqual(
    normalizePoolHostActionResult({
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: true,
      code: "ok",
      pool: { poolId: "daily-coding", revision: 8 },
    }),
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: true,
      code: "ok",
      pool: { poolId: "daily-coding", revision: 8 },
    },
  );
  assert.deepEqual(
    normalizePoolHostActionResult({
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: false,
      code: "revision_conflict",
    }),
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: false,
      code: "revision_conflict",
    },
  );

  for (const invalid of [
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: false,
      code: "revision_conflict",
      path: "/private/config",
    },
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: false,
      code: "private backend said token=CANARY",
    },
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.edit",
      ok: true,
      code: "ok",
      pool: { poolId: "daily-coding", revision: 0 },
    },
  ]) {
    assert.equal(normalizePoolHostActionResult(invalid), null);
  }
});

test("builds exact public create, edit, and remove Pool actions with CAS", () => {
  const pool = {
    poolId: "daily-coding",
    strategy: "quota-aware",
    members: [
      { displayId: "acct_0123456789ab", priority: 10, weight: 2 },
      { displayId: "acct_fedcba987654", priority: 20, weight: 1 },
    ],
    crossProviderFallback: false,
    crossModelFallback: true,
    crossRegionFallback: false,
  };

  assert.deepEqual(buildPoolHostAction("accountPool.create", pool, null), {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.create",
    expectedRevision: null,
    pool,
  });
  assert.deepEqual(buildPoolHostAction("accountPool.edit", pool, 7), {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.edit",
    expectedRevision: 7,
    pool,
  });
  assert.deepEqual(
    buildPoolHostAction("accountPool.remove", { poolId: "daily-coding" }, 7),
    {
      apiVersion: "host-action.openusage/v1",
      action: "accountPool.remove",
      expectedRevision: 7,
      pool: { poolId: "daily-coding" },
    },
  );

  for (const invalid of [
    buildPoolHostAction("accountPool.create", { ...pool, accountId: "private" }, null),
    buildPoolHostAction("accountPool.edit", pool, null),
    buildPoolHostAction("accountPool.remove", { poolId: "daily-coding", token: "x" }, 7),
    buildPoolHostAction("accountPool.create", {
      ...pool,
      members: [{ ...pool.members[0], credential: "CANARY" }],
    }, null),
    buildPoolHostAction("accountPool.create", {
      ...pool,
      members: [pool.members[0], pool.members[0]],
    }, null),
  ]) {
    assert.equal(invalid, null);
  }
});
