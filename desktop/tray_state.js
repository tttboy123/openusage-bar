"use strict";

function capacityProviders(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }
  if (payload && Array.isArray(payload.providers)) {
    return payload.providers;
  }
  return [];
}

function balanceRows(payload) {
  const balances = Array.isArray(payload)
    ? payload
    : payload && Array.isArray(payload.balances)
      ? payload.balances
      : [];
  const selected = new Map();
  for (const balance of balances) {
    if (!balance || balance.state === "unknown" || balance.available == null) {
      continue;
    }
    const key = [
      balance.providerId,
      String(balance.currency ?? "").toUpperCase(),
      String(balance.available),
    ].join("\u0000");
    const current = selected.get(key);
    if (!current || balancePriority(balance) > balancePriority(current)) {
      selected.set(key, balance);
    }
  }
  return [...selected.values()];
}

function balancePriority(balance) {
  const quality = String(balance.quality ?? "").toLowerCase();
  const direct = quality === "direct" ? 2 : quality === "derived" ? 1 : 0;
  const freshness = Number(balance.freshnessSeconds);
  return direct * 1_000_000_000 - (Number.isFinite(freshness) ? freshness : 0);
}

module.exports = {
  balanceRows,
  capacityProviders,
};
