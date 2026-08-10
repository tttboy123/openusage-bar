"use strict";

const crypto = require("crypto");
const path = require("path");

const API_VERSION = "gateway-account-host.openusage/v1";
const READY_DEADLINE_MS = 3_000;
const OPERATION_LIFETIME_MS = 10 * 60 * 1_000;
const TERMINAL_TTL_MS = 10 * 60 * 1_000;
const MAX_OPERATION_RECORDS = 32;
const MAX_PROTOCOL_BYTES = 8 * 1024;

function createGatewayAccountOperationHost({
  executor,
  spawnProcess,
  randomBytes = crypto.randomBytes,
  readyDeadlineMs = READY_DEADLINE_MS,
  lifetimeMs = OPERATION_LIFETIME_MS,
  terminalTtlMs = TERMINAL_TTL_MS,
  maxRecords = MAX_OPERATION_RECORDS,
  now = Date.now,
  platform = process.platform,
} = {}) {
  let active = null;
  const operations = new Map();
  const boundedTtlMs = validTerminalTtl(terminalTtlMs);
  const boundedMaxRecords = validMaxRecords(maxRecords);
  const clock = typeof now === "function" ? now : Date.now;

  const pruneExpired = () => {
    const current = clock();
    for (const [operationId, record] of operations) {
      if (
        record.terminalAt !== null &&
        Number.isFinite(current) &&
        current - record.terminalAt >= boundedTtlMs
      ) {
        operations.delete(operationId);
      }
    }
  };

  const makeRoom = () => {
    pruneExpired();
    while (operations.size >= boundedMaxRecords) {
      const terminal = [...operations.entries()].find(
        ([, record]) => record.terminalAt !== null,
      );
      if (terminal === undefined) return false;
      operations.delete(terminal[0]);
    }
    return true;
  };

  return {
    open(intent) {
      if (active !== null) return Promise.resolve({ kind: "busy" });
      if (
        !validExecutor(executor, platform) ||
        typeof spawnProcess !== "function" ||
        typeof randomBytes !== "function"
      ) {
        return Promise.resolve({ kind: "unavailable" });
      }
      if (!makeRoom()) return Promise.resolve({ kind: "busy" });
      const operationId = nextOperationId(randomBytes, operations);
      if (operationId === null) {
        return Promise.resolve({ kind: "unavailable" });
      }
      let child;
      try {
        child = spawnProcess(executor.command, executor.args, {
          shell: false,
          stdio: ["pipe", "pipe", "pipe"],
          windowsHide: true,
        });
      } catch {
        return Promise.resolve({ kind: "unavailable" });
      }
      if (!validChild(child)) {
        terminate(child);
        return Promise.resolve({ kind: "unavailable" });
      }
      const record = {
        child,
        operationId,
        state: "pending",
        code: null,
        terminalAt: null,
      };
      active = record;
      operations.set(operationId, record);
      return new Promise((resolve) => {
        let readySettled = false;
        let opened = false;
        let terminalResult = null;
        let stdout = "";
        let stdoutBytes = 0;
        let stderrBytes = 0;
        let lifetimeTimer;
        const finalize = (state, code) => {
          if (record.state !== "pending") return;
          record.state = state;
          record.code = code;
          record.terminalAt = clock();
          if (active === record) active = null;
          record.child = null;
          clearTimeout(lifetimeTimer);
        };
        const finishBeforeReady = (result) => {
          if (readySettled) return;
          readySettled = true;
          clearTimeout(readyTimer);
          if (result.kind !== "opened") {
            clearTimeout(lifetimeTimer);
            terminate(child);
            if (active?.child === child) active = null;
            operations.delete(operationId);
          }
          resolve(result);
        };
        const fail = () => {
          if (!opened) {
            finishBeforeReady({ kind: "unavailable" });
            return;
          }
          finalize("failed", "service_unavailable");
          terminate(child);
        };
        const readyTimer = setTimeout(fail, validReadyDeadline(readyDeadlineMs));
        if (typeof readyTimer.unref === "function") readyTimer.unref();
        lifetimeTimer = setTimeout(() => {
          if (!opened) {
            fail();
            return;
          }
          finalize("timed_out", "timed_out");
          terminate(child);
        }, validLifetime(lifetimeMs));
        if (typeof lifetimeTimer.unref === "function") lifetimeTimer.unref();
        child.stdout.on("data", (chunk) => {
          if (!Buffer.isBuffer(chunk)) {
            fail();
            return;
          }
          stdoutBytes += chunk.length;
          if (stdoutBytes > MAX_PROTOCOL_BYTES) {
            fail();
            return;
          }
          stdout += chunk.toString("utf8");
          while (stdout.includes("\n")) {
            const newline = stdout.indexOf("\n");
            const line = stdout.slice(0, newline);
            stdout = stdout.slice(newline + 1);
            const envelope = parseProtocolEnvelope(line);
            if (!opened) {
              if (envelope?.event !== "ready") {
                fail();
                return;
              }
              opened = true;
              finishBeforeReady({
                kind: "opened",
                response: {
                  apiVersion: API_VERSION,
                  operationId,
                  state: "opened",
                },
              });
              continue;
            }
            if (terminalResult !== null || envelope?.event !== "terminal") {
              fail();
              return;
            }
            terminalResult = envelope;
          }
        });
        child.stderr.on("data", (chunk) => {
          if (!Buffer.isBuffer(chunk)) {
            fail();
            return;
          }
          stderrBytes += chunk.length;
          if (stderrBytes > MAX_PROTOCOL_BYTES) fail();
        });
        child.once("error", fail);
        child.once("close", (code, signal) => {
          if (active?.child === child) active = null;
          if (!opened) {
            fail();
            return;
          }
          if (record.state !== "pending") return;
          if (
            stdout !== "" ||
            signal !== null ||
            terminalResult === null ||
            !validTerminalExit(terminalResult.state, code)
          ) {
            finalize("failed", "service_unavailable");
            return;
          }
          finalize(terminalResult.state, terminalResult.code);
        });
        try {
          child.stdin.write(Buffer.from(JSON.stringify(intent), "utf8"));
          child.stdin.end();
        } catch {
          fail();
        }
      });
    },
    poll(operationId) {
      pruneExpired();
      const record = operations.get(operationId);
      if (record === undefined) return null;
      const result = {
        apiVersion: API_VERSION,
        operationId,
        state: record.state,
      };
      if (record.code !== null) result.code = record.code;
      return result;
    },
  };
}

function parseProtocolEnvelope(line) {
  try {
    const value = JSON.parse(line);
    if (
      value === null ||
      typeof value !== "object" ||
      Array.isArray(value) ||
      Object.getPrototypeOf(value) !== Object.prototype
    ) {
      return null;
    }
    const keys = Object.keys(value);
    const protocolKeys = line.match(/"(?:version|event|state|code)"\s*:/gu) ?? [];
    if (
      keys.length === 2 &&
      keys.includes("version") &&
      keys.includes("event") &&
      value.version === 1 &&
      value.event === "ready" &&
      protocolKeys.length === 2
    ) {
      return { event: "ready" };
    }
    if (
      keys.length === 4 &&
      keys.includes("version") &&
      keys.includes("event") &&
      keys.includes("state") &&
      keys.includes("code") &&
      value.version === 1 &&
      value.event === "terminal" &&
      protocolKeys.length === 4 &&
      validTerminalPair(value.state, value.code)
    ) {
      return { event: "terminal", state: value.state, code: value.code };
    }
  } catch {
    return null;
  }
  return null;
}

function validTerminalPair(state, code) {
  if (state === "succeeded") return code === "ok";
  if (state === "cancelled") return code === "cancelled";
  if (state === "timed_out") return code === "timed_out";
  return (
    state === "failed" &&
    [
      "invalid_intent",
      "not_found",
      "already_exists",
      "invalid_input",
      "credential_unavailable",
      "config_write_failed",
      "account_in_use",
      "helper_busy",
      "service_unavailable",
    ].includes(code)
  );
}

function validTerminalExit(state, exitCode) {
  return state === "succeeded" || state === "cancelled"
    ? exitCode === 0
    : exitCode === 1;
}

function validExecutor(value, platform) {
  return isGatewayAccountEditorExecutor(value, platform);
}

function isGatewayAccountEditorExecutor(value, platform = process.platform) {
  try {
    if (
      value === null ||
      typeof value !== "object" ||
      Array.isArray(value) ||
      Object.getPrototypeOf(value) !== Object.prototype
    ) {
      return false;
    }
    const descriptors = Object.getOwnPropertyDescriptors(value);
    if (
      Reflect.ownKeys(descriptors).length !== 2 ||
      !dataDescriptor(descriptors.command) ||
      !dataDescriptor(descriptors.args)
    ) {
      return false;
    }
    const command = descriptors.command.value;
    const args = descriptors.args.value;
    const pathApi = platform === "win32"
      ? path.win32
      : platform === "darwin" || platform === "linux"
        ? path.posix
        : null;
    if (
      typeof command !== "string" ||
      command.length === 0 ||
      command.length > 4096 ||
      /[\u0000-\u001f\u007f]/u.test(command) ||
      pathApi === null ||
      !pathApi.isAbsolute(command) ||
      (platform === "win32" && command.startsWith("\\\\")) ||
      !Array.isArray(args) ||
      Object.getPrototypeOf(args) !== Array.prototype
    ) {
      return false;
    }
    const argDescriptors = Object.getOwnPropertyDescriptors(args);
    return (
      Reflect.ownKeys(argDescriptors).length === 2 &&
      dataDescriptor(argDescriptors[0]) &&
      argDescriptors[0].value === "gateway-account-editor" &&
      dataDescriptor(argDescriptors.length) &&
      argDescriptors.length.value === 1
    );
  } catch {
    return false;
  }
}

function dataDescriptor(value) {
  return (
    value !== undefined &&
    Object.prototype.hasOwnProperty.call(value, "value")
  );
}

function validChild(child) {
  return (
    child !== null &&
    typeof child === "object" &&
    typeof child.once === "function" &&
    typeof child.kill === "function" &&
    typeof child.stdin?.write === "function" &&
    typeof child.stdin?.end === "function" &&
    typeof child.stdout?.on === "function" &&
    typeof child.stderr?.on === "function"
  );
}

function nextOperationId(randomBytes, operations) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      const value = randomBytes(16);
      if (!Buffer.isBuffer(value) || value.length !== 16) return null;
      const operationId = `op_${value.toString("hex")}`;
      if (!operations.has(operationId)) return operationId;
    } catch {
      return null;
    }
  }
  return null;
}

function validReadyDeadline(value) {
  return Number.isInteger(value) && value >= 50 && value <= READY_DEADLINE_MS
    ? value
    : READY_DEADLINE_MS;
}

function validLifetime(value) {
  return Number.isInteger(value) && value >= 50 && value <= OPERATION_LIFETIME_MS
    ? value
    : OPERATION_LIFETIME_MS;
}

function validTerminalTtl(value) {
  return Number.isInteger(value) && value >= 50 && value <= TERMINAL_TTL_MS
    ? value
    : TERMINAL_TTL_MS;
}

function validMaxRecords(value) {
  return Number.isInteger(value) && value >= 1 && value <= MAX_OPERATION_RECORDS
    ? value
    : MAX_OPERATION_RECORDS;
}

function terminate(child) {
  try {
    child?.kill();
  } catch {
    // The public boundary remains a stable unavailable result.
  }
}

module.exports = {
  createGatewayAccountOperationHost,
  isGatewayAccountEditorExecutor,
};
