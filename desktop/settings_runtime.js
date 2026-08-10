"use strict";

const fs = require("fs");
const path = require("path");

const MAX_RUNTIME_PATH_LENGTH = 4096;
const HOST_ACTION_ARGS = Object.freeze(["gateway-account-mutate"]);

function resolveHostActionExecutor({
  isPackaged = false,
  resourcesPath,
  platform = process.platform,
  pathExists = fs.existsSync,
} = {}) {
  try {
    if (isPackaged !== true || typeof pathExists !== "function") return null;
    const pathApi = platformPath(platform);
    if (pathApi === null) return null;
    const root = validatedAbsolutePath(resourcesPath, pathApi);
    if (root === null) return null;
    const command = settingsHelperCommand(root, platform, pathApi);
    if (command === null || pathExists(command) !== true) return null;
    return { command, args: [...HOST_ACTION_ARGS] };
  } catch {
    // Host action mutation is a trusted packaged-helper boundary. Fail closed
    // without reflecting filesystem or environment details to the renderer.
    return null;
  }
}

function settingsHelperCommand(resourcesPath, platform, pathApi) {
  if (platform === "darwin") {
    return pathApi.join(resourcesPath, "settings", "openusage-settings");
  }
  if (platform === "win32") {
    return pathApi.join(resourcesPath, "settings", "openusage-settings.exe");
  }
  if (platform === "linux") {
    return pathApi.join(resourcesPath, "settings", "openusage-settings");
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

module.exports = { resolveHostActionExecutor };
