import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const capacity = fs.readFileSync(path.join(root, "src/pages/CapacityPage.tsx"), "utf8");
const app = fs.readFileSync(path.join(root, "src/App.tsx"), "utf8");
const api = fs.readFileSync(path.join(root, "src/api.ts"), "utf8");
const desktopMain = fs.readFileSync(path.join(root, "../desktop/main.js"), "utf8");
const collectorRuntime = fs.readFileSync(
  path.join(root, "../desktop/collector_runtime.js"),
  "utf8",
);
const i18n = fs.readFileSync(path.join(root, "src/i18n.ts"), "utf8");
const css = fs.readFileSync(path.join(root, "src/styles/app.css"), "utf8");

test("stale capacity is distinct from low and exhausted capacity", () => {
  assert.match(capacity, /item\.stale === true \|\| value === "stale"/u);
  assert.match(capacity, /value === "rate_limited" \|\| item\.remainingRatio === 0/u);
  assert.doesNotMatch(capacity, /value === "low" \|\| value === "stale"/u);
  assert.match(i18n, /capacityStale: "Expired · waiting for refresh"/u);
  assert.match(i18n, /capacityStale: "已过期 · 等待刷新"/u);
  for (const className of ["pill-low", "pill-stale", "pill-error", "pill-exhausted"]) {
    assert.match(css, new RegExp(`\\.${className}\\b`, "u"));
  }
});

test("entering Capacity uses the trusted host refresh and Local API stays read-only", () => {
  assert.match(app, /location\.pathname === "\/capacity"/u);
  assert.match(app, /refreshInFlightRef\.current/u);
  assert.match(api, /"\/host\/v1\/actions"/u);
  assert.match(api, /action: "usage\.refresh"/u);
  assert.doesNotMatch(api, /fetch\("\/v1\/refresh"/u);
  assert.match(app, /aria-busy=\{refreshing\}/u);
  assert.match(app, /role="status" aria-live="polite"/u);
});

test("the user refresh deadline covers the bounded five-minute collector window", () => {
  assert.match(collectorRuntime, /MAX_COLLECTOR_COMMAND_TIMEOUT_MS = 300_000/u);
  assert.match(collectorRuntime, /attempts = 150/u);
  assert.match(collectorRuntime, /attempts > 150/u);
  assert.match(desktopMain, /timeoutMs: 300_000/u);
  assert.match(api, /HOST_REFRESH_DEADLINE_MS = 305_000/u);
});
