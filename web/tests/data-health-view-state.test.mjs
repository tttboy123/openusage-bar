import assert from "node:assert/strict";
import test from "node:test";
import { dataHealthEmptyState } from "../.test-dist/dataHealthViewState.js";


test("no sources takes priority for a valid empty source collection", () => {
  assert.equal(dataHealthEmptyState(0, 0, "all"), "no_sources");
  assert.equal(dataHealthEmptyState(0, 0, "issues"), "no_sources");
});


test("issues filter distinguishes no issues from non-empty results", () => {
  assert.equal(dataHealthEmptyState(12, 0, "issues"), "no_issues");
  assert.equal(dataHealthEmptyState(12, 0, "all"), "none");
  assert.equal(dataHealthEmptyState(12, 3, "issues"), "none");
  assert.equal(dataHealthEmptyState(12, 3, "all"), "none");
});


test("invalid or contradictory facts fail closed to unknown", () => {
  const invalidCases = [
    [-1, 0, "all"],
    [1, -1, "issues"],
    [0, 1, "issues"],
    [2, 3, "issues"],
    [1.5, 0, "all"],
    [2, 0.5, "issues"],
    [Number.NaN, 0, "all"],
    [Number.POSITIVE_INFINITY, 0, "all"],
    [Number.MAX_SAFE_INTEGER + 1, 0, "all"],
    [Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER + 1, "issues"],
    ["2", 0, "all"],
    [2, false, "issues"],
    [2, 0, "future-filter"],
  ];

  for (const [totalSources, issueCount, filter] of invalidCases) {
    assert.equal(
      dataHealthEmptyState(totalSources, issueCount, filter),
      "unknown",
      JSON.stringify({ totalSources, issueCount, filter }),
    );
  }
});


test("the JavaScript safe-integer boundary remains valid", () => {
  assert.equal(
    dataHealthEmptyState(
      Number.MAX_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
      "issues",
    ),
    "none",
  );
});
