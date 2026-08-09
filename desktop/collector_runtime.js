"use strict";

const fs = require("fs");
const path = require("path");

const MAX_RUNTIME_PATH_LENGTH = 4096;

function resolveCollectorCommand({
  isPackaged = false,
  resourcesPath,
  platform = process.platform,
  environment = process.env,
  pathExists = fs.existsSync,
  developmentCandidates = [],
} = {}) {
  try {
    const pathApi = platformPath(platform);
    if (pathApi === null || typeof pathExists !== "function") return null;

    if (isPackaged === true) {
      const root = validatedAbsolutePath(resourcesPath, pathApi);
      if (root === null) return null;
      const executableName =
        platform === "win32" ? "openusage-collector.exe" : "openusage-collector";
      const candidate = pathApi.join(root, "collector", executableName);
      return pathExists(candidate) === true ? candidate : null;
    }

    const candidates = [];
    const override = environment?.USAGEHUB_COLLECTOR;
    if (override !== undefined) candidates.push(override);
    if (Array.isArray(developmentCandidates)) {
      candidates.push(...developmentCandidates);
    }
    for (const value of candidates) {
      const candidate = validatedAbsolutePath(value, pathApi);
      if (candidate !== null && pathExists(candidate) === true) {
        return candidate;
      }
    }
  } catch {
    // Runtime discovery is a fail-closed boundary. Do not reflect a hostile
    // environment value or filesystem error into the renderer.
  }
  return null;
}

function platformPath(platform) {
  if (platform === "win32") return path.win32;
  if (platform === "darwin" || platform === "linux") return path.posix;
  return null;
}

function validatedAbsolutePath(value, pathApi) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_RUNTIME_PATH_LENGTH ||
    /[\u0000-\u001f\u007f]/u.test(value) ||
    !pathApi.isAbsolute(value) ||
    value.split(/[\\/]/u).includes("..")
  ) {
    return null;
  }
  const normalized = pathApi.normalize(value);
  return normalized && pathApi.isAbsolute(normalized) ? normalized : null;
}

module.exports = { resolveCollectorCommand };
