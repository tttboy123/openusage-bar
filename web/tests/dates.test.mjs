import assert from "node:assert/strict";
import test from "node:test";
import { localDayKey } from "../.test-dist/dates.js";

test("localDayKey pads month and day", () => {
  assert.equal(localDayKey(new Date(2026, 0, 1)), "2026-01-01");
  assert.equal(localDayKey(new Date(2026, 11, 31)), "2026-12-31");
});

test("localDayKey uses local calendar date", () => {
  const d = new Date(2026, 6, 7, 23, 30);
  assert.equal(localDayKey(d), "2026-07-07");
});
