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

module.exports = {
  capacityProviders,
};
