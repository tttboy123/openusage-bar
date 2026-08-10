import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { sanitizeHostActionRequest } = require("../gateway_proxy.js");

function requireSanitizer() {
  assert.equal(
    typeof sanitizeHostActionRequest,
    "function",
    "sanitizeHostActionRequest must be exported from gateway_proxy.js",
  );
  return sanitizeHostActionRequest;
}

function assertNoPrivateFields(value) {
  const serialized = JSON.stringify(value);
  assert.equal(
    /credential|accountId|account_id|path|token|header|bearer|authorization|cookie|secret/iu.test(
      serialized,
    ),
    false,
  );
}

test("host-action sanitizer maps a public Account Pool create envelope to a private-command-safe object", () => {
  const sanitize = requireSanitizer();

  const request = {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.create",
    expectedRevision: null,
    pool: {
      poolId: "daily-coding",
      strategy: "quota-aware",
      members: [
        { displayId: "acct_0123456789ab", priority: 10, weight: 2 },
        { displayId: "acct_fedcba987654", priority: 20, weight: 1 },
      ],
      crossProviderFallback: false,
      crossModelFallback: false,
      crossRegionFallback: false,
    },
  };

  const sanitized = sanitize(request);

  assert.deepEqual(sanitized, {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.create",
    command: {
      version: 1,
      action: "create_pool",
      pool: {
        poolId: "daily-coding",
        strategy: "quota-aware",
        members: [
          { displayId: "acct_0123456789ab", priority: 10, weight: 2 },
          { displayId: "acct_fedcba987654", priority: 20, weight: 1 },
        ],
        crossProviderFallback: false,
        crossModelFallback: false,
        crossRegionFallback: false,
      },
      expectedRevision: null,
    },
  });
  assertNoPrivateFields(sanitized);
});

test("host-action sanitizer rejects credential injection and hostile Account Pool create envelopes", () => {
  const sanitize = requireSanitizer();
  const safeRequest = {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.create",
    expectedRevision: null,
    pool: {
      poolId: "daily-coding",
      strategy: "quota-aware",
      members: [{ displayId: "acct_0123456789ab", priority: 10, weight: 2 }],
      crossProviderFallback: false,
      crossModelFallback: false,
      crossRegionFallback: false,
    },
  };
  const accessorRequest = structuredClone(safeRequest);
  Object.defineProperty(accessorRequest.pool, "strategy", {
    enumerable: true,
    get() {
      throw new Error("must not invoke hostile accessors");
    },
  });

  const hostileRequests = [
    { ...safeRequest, credentialStoreAccount: "login.keychain" },
    { ...safeRequest, pool: { ...safeRequest.pool, token: "sk-private" } },
    {
      ...safeRequest,
      pool: {
        ...safeRequest.pool,
        members: [
          {
            displayId: "acct_0123456789ab",
            priority: 10,
            weight: 2,
            accountId: "real-private-account",
          },
        ],
      },
    },
    {
      ...safeRequest,
      pool: {
        ...safeRequest.pool,
        members: [
          { displayId: "acct_0123456789ab", priority: 10, weight: 2 },
          { displayId: "acct_0123456789ab", priority: 20, weight: 1 },
        ],
      },
    },
    { ...safeRequest, pool: { ...safeRequest.pool, credentialHeader: "Bearer x" } },
    accessorRequest,
  ];

  for (const hostileRequest of hostileRequests) {
    assert.equal(sanitize(hostileRequest), null);
  }
});

test("host-action sanitizer maps Account Pool edit with CAS revision", () => {
  const sanitize = requireSanitizer();

  const sanitized = sanitize({
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.edit",
    expectedRevision: 7,
    pool: {
      poolId: "daily-coding",
      strategy: "round-robin",
      members: [{ displayId: "acct_0123456789ab", priority: 0, weight: 100 }],
      crossProviderFallback: true,
      crossModelFallback: false,
      crossRegionFallback: true,
    },
  });

  assert.deepEqual(sanitized, {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.edit",
    command: {
      version: 1,
      action: "edit_pool",
      pool: {
        poolId: "daily-coding",
        strategy: "round-robin",
        members: [{ displayId: "acct_0123456789ab", priority: 0, weight: 100 }],
        crossProviderFallback: true,
        crossModelFallback: false,
        crossRegionFallback: true,
      },
      expectedRevision: 7,
    },
  });
  assertNoPrivateFields(sanitized);
});

test("host-action sanitizer maps Account Pool remove with CAS revision", () => {
  const sanitize = requireSanitizer();

  const sanitized = sanitize({
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.remove",
    expectedRevision: 7,
    pool: { poolId: "daily-coding" },
  });

  assert.deepEqual(sanitized, {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.remove",
    command: {
      version: 1,
      action: "remove_pool",
      pool: { poolId: "daily-coding" },
      expectedRevision: 7,
    },
  });
  assertNoPrivateFields(sanitized);
});

test("host-action sanitizer rejects Account Pool edit and remove without exact CAS envelopes", () => {
  const sanitize = requireSanitizer();
  const editRequest = {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.edit",
    expectedRevision: 7,
    pool: {
      poolId: "daily-coding",
      strategy: "round-robin",
      members: [{ displayId: "acct_0123456789ab", priority: 0, weight: 100 }],
      crossProviderFallback: true,
      crossModelFallback: false,
      crossRegionFallback: true,
    },
  };
  const removeRequest = {
    apiVersion: "host-action.openusage/v1",
    action: "accountPool.remove",
    expectedRevision: 7,
    pool: { poolId: "daily-coding" },
  };

  for (const hostileRequest of [
    { ...editRequest, expectedRevision: null },
    { ...editRequest, expectedRevision: 0 },
    { ...editRequest, pool: { ...editRequest.pool, credentialStoreAccount: "x" } },
    { ...removeRequest, expectedRevision: null },
    { ...removeRequest, expectedRevision: 0 },
    { ...removeRequest, pool: { ...removeRequest.pool, members: [] } },
    { ...removeRequest, pool: { ...removeRequest.pool, token: "sk-private" } },
  ]) {
    assert.equal(sanitize(hostileRequest), null);
  }
});
