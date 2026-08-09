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

test("normalizes only the renderer-safe account pool public shape", () => {
  const normalized = normalizeAccountPools(accountPoolsPayload());

  assert.deepEqual(normalized, {
    accounts: [
      {
        alias: "Team primary",
        displayId: "acct_000000007f3a",
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
      account({ displayId: "acct_000000000002", status: "future_status" }),
      account({ displayId: "acct_000000000001", status: "disabled", weight: 0 }),
      account({
        displayId: "acct_000000000003",
        status: "backend_unavailable",
        cooldown: cooldown({ state: "unknown" }),
      }),
    ],
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

test("builds a stable empty or unknown view model without reflecting hostile input", () => {
  assert.deepEqual(accountPoolsViewModel({ accounts: [] }), {
    state: "empty",
    accounts: [],
    summary: {
      total: 0,
      disabled: 0,
      backendUnavailable: 0,
      unknown: 0,
    },
  });

  const model = accountPoolsViewModel({
    accounts: [],
    credentialStoreAccount: "CANARY_CREDENTIAL_STORE_ACCOUNT",
  });
  assert.deepEqual(model, {
    state: "unknown",
    accounts: [],
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
    ...overrides,
  };
}

function account(overrides = {}) {
  return {
    alias: "Team primary",
    displayId: "acct_000000007f3a",
    status: "ready",
    quota: quota(),
    cooldown: cooldown(),
    pools: [membership(), membership({ poolId: "overflow", priority: 30, weight: 20 })],
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
