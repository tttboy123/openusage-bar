import assert from "node:assert/strict";
import test from "node:test";

import {
  accountPoolsViewModel,
  normalizeAccountPools,
} from "../.test-dist/accountPools.js";

const PRIVATE_CANARIES = {
  credentialStoreAccount: "CANARY_CREDENTIAL_STORE_ACCOUNT",
  externalOpaqueRef: "CANARY_EXTERNAL_OPAQUE_REF",
  token: "CANARY_TOKEN",
  path: "CANARY_PATH",
  header: "CANARY_HEADER",
};

test("normalizes the exact public account and Pool projection", () => {
  const payload = accountPoolsPayload();
  payload.accounts = payload.accounts.map((item) => ({
    ...item,
    providerId: "openai",
  }));
  payload.accounts[0].pools = [
    { poolId: "default", priority: 10, weight: 80 },
  ];
  payload.pools = [
    {
      poolId: "default",
      revision: 3,
      strategy: "quota-aware",
      members: [
        { displayId: "acct_000000007f3a", priority: 10, weight: 80 },
        { displayId: "acct_000000000001", priority: 30, weight: 20 },
      ],
      crossProviderFallback: false,
      crossModelFallback: true,
      crossRegionFallback: false,
    },
  ];

  assert.deepEqual(normalizeAccountPools(payload), {
    accounts: payload.accounts,
    pools: payload.pools,
  });
  assert.doesNotMatch(JSON.stringify(normalizeAccountPools(payload)), /accountId|credential|token|path|header/iu);
});

test("normalizes only the renderer-safe account pool public shape", () => {
  const normalized = normalizeAccountPools(accountPoolsPayload());

  assert.deepEqual(normalized, {
    accounts: [
      {
        alias: "Team primary",
        displayId: "acct_000000007f3a",
        providerId: "openai",
        status: "ready",
        quota: {
          state: "available",
          remaining: 42000,
          limit: 100000,
          resetAt: "2026-08-10T12:00:00Z",
        },
        cooldown: {
          state: "inactive",
          until: null,
        },
        pools: [
          {
            poolId: "default",
            priority: 10,
            weight: 80,
          },
          {
            poolId: "overflow",
            priority: 30,
            weight: 20,
          },
        ],
        priority: 10,
        weight: 80,
      },
      {
        alias: null,
        displayId: "acct_000000000001",
        providerId: "openai",
        status: "disabled",
        quota: {
          state: "unknown",
          remaining: null,
          limit: null,
          resetAt: null,
        },
        cooldown: {
          state: "unknown",
          until: null,
        },
        pools: [],
        priority: 100,
        weight: 0,
      },
    ],
    pools: [
      {
        poolId: "default",
        revision: 1,
        strategy: "fixed-first",
        members: [
          { displayId: "acct_000000007f3a", priority: 10, weight: 80 },
        ],
        crossProviderFallback: false,
        crossModelFallback: false,
        crossRegionFallback: false,
      },
      {
        poolId: "overflow",
        revision: 1,
        strategy: "fixed-first",
        members: [
          { displayId: "acct_000000007f3a", priority: 30, weight: 20 },
        ],
        crossProviderFallback: false,
        crossModelFallback: false,
        crossRegionFallback: false,
      },
    ],
  });

  const serialized = JSON.stringify(normalized);
  for (const canary of Object.values(PRIVATE_CANARIES)) {
    assert.doesNotMatch(serialized, new RegExp(canary));
  }
});

test("rejects credential, external reference, token, path, and header fields", () => {
  for (const [field, canary] of Object.entries(PRIVATE_CANARIES)) {
    const rootLeak = accountPoolsPayload();
    rootLeak[field] = canary;
    assert.equal(normalizeAccountPools(rootLeak), null, `root ${field}`);

    const accountLeak = accountPoolsPayload();
    accountLeak.accounts[0][field] = canary;
    assert.equal(normalizeAccountPools(accountLeak), null, `account ${field}`);

    const quotaLeak = accountPoolsPayload();
    quotaLeak.accounts[0].quota[field] = canary;
    assert.equal(normalizeAccountPools(quotaLeak), null, `quota ${field}`);

    const cooldownLeak = accountPoolsPayload();
    cooldownLeak.accounts[0].cooldown[field] = canary;
    assert.equal(normalizeAccountPools(cooldownLeak), null, `cooldown ${field}`);

    const poolLeak = accountPoolsPayload();
    poolLeak.accounts[0].pools[0][field] = canary;
    assert.equal(normalizeAccountPools(poolLeak), null, `pool ${field}`);
  }
});

test("distinguishes unknown, disabled, and backend unavailable states", () => {
  const payload = accountPoolsPayload({
    accounts: [
      account({ displayId: "acct_000000000002", status: "future_status", pools: [] }),
      account({ displayId: "acct_000000000001", status: "disabled", weight: 0, pools: [] }),
      account({
        displayId: "acct_000000000003",
        status: "backend_unavailable",
        cooldown: cooldown({ state: "unknown" }),
        pools: [],
      }),
    ],
    pools: [],
  });

  assert.deepEqual(normalizeAccountPools(payload)?.accounts.map((item) => item.status), [
    "unknown",
    "disabled",
    "backend_unavailable",
  ]);

  const model = accountPoolsViewModel(payload);
  assert.deepEqual(
    model.accounts.map(({ displayId, statusKey, statusTone }) => ({
      displayId,
      statusKey,
      statusTone,
    })),
    [
      {
        displayId: "acct_000000000002",
        statusKey: "accountPoolStatusUnknown",
        statusTone: "unknown",
      },
      {
        displayId: "acct_000000000001",
        statusKey: "accountPoolStatusDisabled",
        statusTone: "neutral",
      },
      {
        displayId: "acct_000000000003",
        statusKey: "accountPoolStatusBackendUnavailable",
        statusTone: "warning",
      },
    ],
  );
  assert.deepEqual(model.summary, {
    total: 3,
    disabled: 1,
    backendUnavailable: 1,
    unknown: 1,
  });
});

test("fails closed for accessors, symbols, and invalid account facts", () => {
  const accessor = accountPoolsPayload();
  Object.defineProperty(accessor.accounts[0], "displayId", {
    get() {
      throw new Error("must not invoke getter");
    },
    enumerable: true,
  });
  assert.equal(normalizeAccountPools(accessor), null);

  const withSymbol = accountPoolsPayload();
  withSymbol.accounts[0][Symbol("token")] = "CANARY_SYMBOL";
  assert.equal(normalizeAccountPools(withSymbol), null);

  assert.equal(
    normalizeAccountPools(
      accountPoolsPayload({
        accounts: [account({ displayId: "acct\nbad" })],
      }),
    ),
    null,
  );
  assert.equal(
    normalizeAccountPools(
      accountPoolsPayload({
        accounts: [account({ displayId: "openai.work.gateway-api-key" })],
      }),
    ),
    null,
  );
  assert.equal(
    normalizeAccountPools(
      accountPoolsPayload({
        accounts: [account({ weight: -1 })],
      }),
    ),
    null,
  );
  assert.equal(
    normalizeAccountPools(
      accountPoolsPayload({
        accounts: [account({ pools: [membership({ priority: 1.5 })] })],
      }),
    ),
    null,
  );
});

test("never invokes hostile account or Pool array accessors", () => {
  let invoked = 0;
  for (const field of ["accounts", "pools"]) {
    const payload = accountPoolsPayload();
    const items = payload[field];
    const original = items[0];
    Object.defineProperty(items, "0", {
      enumerable: true,
      get() {
        invoked += 1;
        return original;
      },
    });
    assert.equal(normalizeAccountPools(payload), null, field);
  }
  assert.equal(invoked, 0);
});

test("builds a stable empty or unknown view model without reflecting hostile input", () => {
  assert.deepEqual(accountPoolsViewModel({ accounts: [], pools: [] }), {
    state: "empty",
    accounts: [],
    pools: [],
    summary: {
      total: 0,
      disabled: 0,
      backendUnavailable: 0,
      unknown: 0,
    },
  });

  const model = accountPoolsViewModel({
    accounts: [],
    pools: [],
    credentialStoreAccount: "CANARY_CREDENTIAL_STORE_ACCOUNT",
  });
  assert.deepEqual(model, {
    state: "unknown",
    accounts: [],
    pools: [],
    summary: {
      total: 0,
      disabled: 0,
      backendUnavailable: 0,
      unknown: 0,
    },
  });
  assert.doesNotMatch(JSON.stringify(model), /CANARY/);
});

function accountPoolsPayload(overrides = {}) {
  return {
    accounts: [
      account(),
      account({
        alias: null,
        displayId: "acct_000000000001",
        status: "disabled",
        quota: quota({ state: "unknown", remaining: null, limit: null, resetAt: null }),
        cooldown: cooldown({ state: "unknown", until: null }),
        pools: [],
        priority: 100,
        weight: 0,
      }),
    ],
    pools: [
      pool(),
      pool({
        poolId: "overflow",
        members: [
          poolMember({
            displayId: "acct_000000007f3a",
            priority: 30,
            weight: 20,
          }),
        ],
      }),
    ],
    ...overrides,
  };
}

function account(overrides = {}) {
  return {
    alias: "Team primary",
    displayId: "acct_000000007f3a",
    providerId: "openai",
    status: "ready",
    quota: quota(),
    cooldown: cooldown(),
    pools: [membership(), membership({ poolId: "overflow", priority: 30, weight: 20 })],
    priority: 10,
    weight: 80,
    ...overrides,
  };
}

function pool(overrides = {}) {
  return {
    poolId: "default",
    revision: 1,
    strategy: "fixed-first",
    members: [poolMember()],
    crossProviderFallback: false,
    crossModelFallback: false,
    crossRegionFallback: false,
    ...overrides,
  };
}

function poolMember(overrides = {}) {
  return {
    displayId: "acct_000000007f3a",
    priority: 10,
    weight: 80,
    ...overrides,
  };
}

function quota(overrides = {}) {
  return {
    state: "available",
    remaining: 42000,
    limit: 100000,
    resetAt: "2026-08-10T12:00:00Z",
    ...overrides,
  };
}

function cooldown(overrides = {}) {
  return {
    state: "inactive",
    until: null,
    ...overrides,
  };
}

function membership(overrides = {}) {
  return {
    poolId: "default",
    priority: 10,
    weight: 80,
    ...overrides,
  };
}
