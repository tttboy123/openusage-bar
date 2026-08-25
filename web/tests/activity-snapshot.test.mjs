import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import {
  deriveActivitySnapshot,
  shouldPreserveActivitySnapshot,
  shouldReplaceActivitySnapshot,
} from "../.test-dist/activitySnapshot.js";

async function loadApiModule() {
  const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const output = ts
    .transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ES2022,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: "api.ts",
    })
    .outputText.replace(/^import[\s\S]*?from\s+["'][^"']+["'];\s*/gmu, "");
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

test("fetchActivity preserves the authoritative response revision metadata", async () => {
  const api = await loadApiModule();
  const controller = new AbortController();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      schemaVersion: "1.0",
      dataRevision: 84,
      generatedAt: "2026-08-25T13:00:00Z",
      rows: [{ day: "2026-08-25", totalTokens: 9 }],
      coverage: [{ day: "2026-08-25", covered: true }],
    }),
  });

  try {
    assert.deepEqual(
      await api.fetchActivity(
        "2025-08-26",
        "2026-08-25",
        undefined,
        undefined,
        controller.signal,
      ),
      {
        schemaVersion: "1.0",
        dataRevision: 84,
        generatedAt: "2026-08-25T13:00:00Z",
        rows: [{ day: "2026-08-25", totalTokens: 9 }],
        coverage: [{ day: "2026-08-25", covered: true }],
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchActivity rejects a revision-less response instead of replacing last-good data", async () => {
  const api = await loadApiModule();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      schemaVersion: "1.0",
      generatedAt: "2026-08-25T13:00:00Z",
      rows: [],
      coverage: [],
    }),
  });

  try {
    await assert.rejects(
      api.fetchActivity("2025-08-26", "2026-08-25"),
      /invalid activity response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("derives one selected-period view while preserving snapshot metadata and daily totals", () => {
  const derived = deriveActivitySnapshot(
    {
      schemaVersion: "1.0",
      dataRevision: 42,
      generatedAt: "2026-08-25T12:34:56Z",
      rows: [
        { day: "2026-08-22", providerId: "codex", modelId: "alpha", totalTokens: 100 },
        { day: "2026-08-23", providerId: "codex", modelId: "alpha", totalTokens: 7 },
        { day: "2026-08-23", providerId: "moonshot", modelId: "beta", totalTokens: 5 },
        { day: "2026-08-24", providerId: "codex", modelId: "alpha", totalTokens: 3 },
      ],
      coverage: [
        { day: "2026-08-22", providerId: "openai", covered: true },
        { day: "2026-08-23", providerId: "moonshot", covered: true },
        { day: "2026-08-24", providerId: "openai", covered: false },
      ],
    },
    { from: "2026-08-23", to: "2026-08-24" },
  );

  assert.deepEqual(
    {
      schemaVersion: derived.schemaVersion,
      dataRevision: derived.dataRevision,
      generatedAt: derived.generatedAt,
    },
    {
      schemaVersion: "1.0",
      dataRevision: 42,
      generatedAt: "2026-08-25T12:34:56Z",
    },
  );
  assert.deepEqual(derived.dayTotals, {
    "2026-08-22": 100,
    "2026-08-23": 12,
    "2026-08-24": 3,
  });
  assert.deepEqual(derived.periodDayTotals, {
    "2026-08-23": 12,
    "2026-08-24": 3,
  });
  assert.equal(derived.periodTotal, 15);
  assert.deepEqual(
    derived.periodRows.map((row) => row.day),
    ["2026-08-23", "2026-08-23", "2026-08-24"],
  );
  assert.deepEqual(
    derived.periodCoverage.map((row) => [row.day, row.covered]),
    [
      ["2026-08-23", true],
      ["2026-08-24", false],
    ],
  );
  assert.equal(derived.periodDayTotals["2026-08-23"], derived.dayTotals["2026-08-23"]);

  const moonshot = deriveActivitySnapshot(
    {
      schemaVersion: "1.0",
      dataRevision: 42,
      generatedAt: "2026-08-25T12:34:56Z",
      rows: derived.rows,
      coverage: derived.coverage,
    },
    { from: "2026-08-23", to: "2026-08-24" },
    { providerId: "moonshot" },
  );
  assert.deepEqual(moonshot.periodDayTotals, { "2026-08-23": 5 });
  assert.equal(moonshot.dayTotals["2026-08-23"], 5);
  assert.deepEqual(
    moonshot.periodCoverage.map((row) => [row.providerId, row.covered]),
    [["moonshot", true]],
  );
});

test("rejects a revision regression across every request scope", () => {
  const current = {
    scopeKey: "2025-08-26:2026-08-25:all",
    range: { from: "2025-08-26", to: "2026-08-25" },
    response: {
      schemaVersion: "1.0",
      dataRevision: 50,
      generatedAt: "2026-08-25T13:00:00Z",
      rows: [],
      coverage: [],
    },
  };
  const regressed = {
    ...current,
    response: { ...current.response, dataRevision: 49 },
  };
  const newFilter = {
    ...regressed,
    scopeKey: "2025-08-26:2026-08-25:anthropic",
  };

  assert.equal(shouldReplaceActivitySnapshot(current, regressed), false);
  assert.equal(shouldReplaceActivitySnapshot(current, newFilter), false);
  assert.equal(
    shouldReplaceActivitySnapshot(current, {
      ...current,
      response: { ...current.response, dataRevision: 51 },
    }),
    true,
  );
});

test("manual refresh and provider changes preserve the last-good snapshot", () => {
  const current = {
    scopeKey: "2025-08-26:2026-08-25:all",
    range: { from: "2025-08-26", to: "2026-08-25" },
    response: {
      schemaVersion: "1.0",
      dataRevision: 50,
      generatedAt: "2026-08-25T13:00:00Z",
      rows: [],
      coverage: [],
    },
  };

  assert.equal(shouldPreserveActivitySnapshot(current), true);
  assert.equal(shouldPreserveActivitySnapshot(null), false);
});

test("GUI refresh updates Activity in place instead of remounting its route", async () => {
  const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const activitySource = await readFile(
    new URL("../src/pages/ActivityPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(appSource, /const revealKey = isActivityRoute/);
  assert.match(appSource, /<Reveal key=\{revealKey\}>/);
  assert.equal((appSource.match(/refreshNonce=\{refreshNonce\}/g) ?? []).length, 2);
  assert.equal((activitySource.match(/\[refreshNonce\]/g) ?? []).length, 2);
  assert.match(activitySource, /const active = inRange\.filter\(\(d\) => d\.total > 0\)\.length/);
  assert.match(
    activitySource,
    /day\.state === "partial"[\s\S]*?value: formatTokenCompact\(day\.total\)/,
  );
});
