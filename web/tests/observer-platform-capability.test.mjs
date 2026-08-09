import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeObserverPlatformCapability,
  observerPlatformViewModel,
} from "../.test-dist/observerPlatformCapability.js";


const windowsPayload = {
  operatingSystem: "windows",
  support: "unsupported",
  supportedSourceCount: 0,
  totalSourceCount: 49,
  reasonCode: "source_level_evidence_unverified",
};


test("preserves a verified zero without presenting it as unknown", () => {
  const capability = normalizeObserverPlatformCapability(windowsPayload);
  assert.deepEqual(capability, windowsPayload);
  assert.deepEqual(observerPlatformViewModel(capability), {
    tone: "neutral",
    stateKey: "observerPlatformUnverified",
    detailKey: "observerPlatformVerifiedCount",
    supportedSourceCount: 0,
    totalSourceCount: 49,
    operatingSystemKey: "platformWindows",
  });
});


test("preserves exact partial source counts for Windows and Linux", () => {
  const cases = [
    {
      payload: {
        operatingSystem: "windows",
        support: "supported",
        supportedSourceCount: 1,
        totalSourceCount: 49,
        reasonCode: "supported_sources_available",
      },
      operatingSystemKey: "platformWindows",
    },
    {
      payload: {
        operatingSystem: "linux",
        support: "supported",
        supportedSourceCount: 2,
        totalSourceCount: 49,
        reasonCode: "supported_sources_available",
      },
      operatingSystemKey: "platformLinux",
    },
  ];

  for (const { payload, operatingSystemKey } of cases) {
    const capability = normalizeObserverPlatformCapability(payload);
    assert.deepEqual(capability, payload);
    assert.deepEqual(observerPlatformViewModel(capability), {
      tone: "positive",
      stateKey: "observerPlatformSupported",
      detailKey: "observerPlatformVerifiedCount",
      supportedSourceCount: payload.supportedSourceCount,
      totalSourceCount: 49,
      operatingSystemKey,
    });
  }
});


test("keeps an unknown runtime distinct from a platform with zero evidence", () => {
  const capability = normalizeObserverPlatformCapability({
    operatingSystem: null,
    support: "unknown",
    supportedSourceCount: null,
    totalSourceCount: 49,
    reasonCode: "runtime_platform_unknown",
  });
  assert.deepEqual(observerPlatformViewModel(capability), {
    tone: "unknown",
    stateKey: "observerPlatformUnknown",
    detailKey: "observerPlatformCountUnknown",
    supportedSourceCount: null,
    totalSourceCount: 49,
    operatingSystemKey: "platformUnknown",
  });
});


test("accepts only closed, internally consistent renderer-safe states", () => {
  const invalid = [
    null,
    {},
    { ...windowsPayload, support: "supported" },
    { ...windowsPayload, supportedSourceCount: -1 },
    { ...windowsPayload, supportedSourceCount: 50 },
    { ...windowsPayload, totalSourceCount: 0 },
    { ...windowsPayload, reasonCode: "private/runtime/path" },
    { ...windowsPayload, token: "renderer-secret" },
    {
      operatingSystem: null,
      support: "unknown",
      supportedSourceCount: 0,
      totalSourceCount: 49,
      reasonCode: "runtime_platform_unknown",
    },
  ];
  for (const payload of invalid) {
    assert.equal(normalizeObserverPlatformCapability(payload), null);
  }
});


test("maps all supported runtime platforms without reflecting raw values", () => {
  const cases = [
    ["macos", "platformMacOS"],
    ["windows", "platformWindows"],
    ["linux", "platformLinux"],
  ];
  for (const [operatingSystem, operatingSystemKey] of cases) {
    const capability = normalizeObserverPlatformCapability({
      operatingSystem,
      support: "supported",
      supportedSourceCount: 1,
      totalSourceCount: 49,
      reasonCode: "supported_sources_available",
    });
    assert.equal(
      observerPlatformViewModel(capability).operatingSystemKey,
      operatingSystemKey,
    );
  }
});
