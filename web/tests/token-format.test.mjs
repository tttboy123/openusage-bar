import assert from "node:assert/strict";
import test from "node:test";

import { formatTokenCompact } from "../.test-dist/tokenFormat.js";

test("formats token counts with locale-independent K/M/B/T suffixes", () => {
  assert.equal(formatTokenCompact(0), "0");
  assert.equal(formatTokenCompact(999), "999");
  assert.equal(formatTokenCompact(1_000), "1K");
  assert.equal(formatTokenCompact(12_345), "12.3K");
  assert.equal(formatTokenCompact(1_000_000), "1M");
  assert.equal(formatTokenCompact(66_863_630_563), "66.9B");
  assert.equal(formatTokenCompact(255_040_000_000), "255B");
  assert.equal(formatTokenCompact(1_250_000_000_000), "1.3T");
});

test("never emits locale-specific Chinese compact units", () => {
  for (const value of [10_000, 100_000_000, 255_040_000_000]) {
    assert.doesNotMatch(formatTokenCompact(value), /[万亿]/);
  }
});

test("handles negative and invalid values predictably", () => {
  assert.equal(formatTokenCompact(-1_250_000), "-1.3M");
  assert.equal(formatTokenCompact(Number.NaN), "—");
  assert.equal(formatTokenCompact(Number.POSITIVE_INFINITY), "—");
});
