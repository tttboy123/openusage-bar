"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { TextDecoder, types } = require("util");
const { composeRendererCapability } = require("./runtime_capability");
const {
  createGatewayAccountOperationHost,
  isGatewayAccountEditorExecutor,
} = require("./gateway_account_operations");

const LOCAL_API_PORT = 17821;
const GATEWAY_PORT = 17823;
const LOOPBACK_HOST = "127.0.0.1";
const RUNTIME_CAPABILITY_MEDIA_TYPE =
  "application/vnd.openusage.runtime-capability+json";
const MAX_TARGET_LENGTH = 8 * 1024;
const MAX_VALIDATOR_LENGTH = 8 * 1024;
const MAX_RESPONSE_HEADER_BYTES = 16 * 1024;
const MAX_RESPONSE_HEADER_COUNT = 64;
const MAX_LOCAL_RESPONSE_BYTES = 4 * 1024 * 1024;
const MAX_CAPABILITY_RESPONSE_BYTES = 64 * 1024;
const MAX_ACCOUNT_POOLS_RESPONSE_BYTES = 64 * 1024;
const MAX_DECISION_TRACES_RESPONSE_BYTES = 1024 * 1024;
const MAX_SHOULD_SEND_REQUEST_BYTES = 8 * 1024;
const MAX_SHOULD_SEND_RESPONSE_BYTES = 8 * 1024;
const MAX_HOST_ACTION_REQUEST_BYTES = 16 * 1024;
const MAX_HOST_ACTION_RESPONSE_BYTES = 8 * 1024;
const DEFAULT_DEADLINE_MS = 5_000;
const WINDOWS_ACL_TIMEOUT_MS = 2_000;
const WINDOWS_ACL_MAX_BUFFER = 8 * 1024;
const MAX_STATIC_TARGET_LENGTH = 8 * 1024;
const MAX_LEGACY_JSON_BYTES = 1024 * 1024;
const MAX_LEGACY_JSON_CHUNKS = 512;
const LEGACY_JSON_DEADLINE_MS = 3_000;
const LEGACY_DASHBOARD_PORT = 17822;
const MAX_SHOULD_SEND_PROVIDER_LENGTH = 128;
const MAX_SHOULD_SEND_MODEL_LENGTH = 256;
const MAX_SHOULD_SEND_WINDOW_LENGTH = 64;
const MAX_SHOULD_SEND_TIMESTAMP_LENGTH = 64;
const MAX_ESTIMATED_TOKENS = 2_147_483_647;

const SHOULD_SEND_DECISIONS = new Set(["yes", "no", "defer"]);
const SHOULD_SEND_REASONS = new Set([
  "approaching_limit",
  "burn_rate_too_high",
  "quota_healthy",
  "quota_low",
  "quota_unknown",
]);
const ACCOUNT_POOL_STATUSES = new Set([
  "ready",
  "cooldown",
  "quota_exhausted",
  "disabled",
  "backend_unavailable",
  "unknown",
]);
const ACCOUNT_QUOTA_STATES = new Set(["available", "exhausted", "unknown"]);
const ACCOUNT_COOLDOWN_STATES = new Set(["active", "inactive", "unknown"]);
const HOST_ACTION_API_VERSION = "host-action.openusage/v1";
const GATEWAY_ACCOUNT_HOST_API_VERSION = "gateway-account-host.openusage/v1";
const GATEWAY_ACCOUNT_OPEN_CREATE = "gatewayAccount.openCreate";
const GATEWAY_ACCOUNT_OPEN_EDIT = "gatewayAccount.openEdit";
const GATEWAY_ACCOUNT_OPEN_REPLACE = "gatewayAccount.openReplace";
const GATEWAY_ACCOUNT_OPEN_REMOVE = "gatewayAccount.openRemove";
const GATEWAY_ACCOUNT_ACTIONS = Object.freeze([
  GATEWAY_ACCOUNT_OPEN_CREATE,
  GATEWAY_ACCOUNT_OPEN_EDIT,
  GATEWAY_ACCOUNT_OPEN_REPLACE,
  GATEWAY_ACCOUNT_OPEN_REMOVE,
]);
const GATEWAY_ACCOUNT_PRESETS = new Set([
  "openai",
  "anthropic",
  "deepseek",
  "openrouter",
]);
const ACCOUNT_POOL_CREATE_ACTION = "accountPool.create";
const PROVIDER_CONFIG_APPLY_ACTION = "providerConfig.apply";
const PROVIDER_CONFIG_MUTATE_VERSION = 1;
const PROVIDER_CONFIG_FIELD_LIMIT = 4096;
const HOST_ACTION_SUBCOMMANDS = new Set([
  "gateway-account-mutate",
  "provider-config-apply",
]);
const PROVIDER_CONFIG_AGENTS = new Set([
  "claude_code",
  "codex",
  "gemini_cli",
  "opencode",
]);
const PROVIDER_CONFIG_CATEGORIES = new Set(["official", "gateway"]);
const PROVIDER_CONFIG_STATUSES = new Set(["created", "updated"]);
const ACCOUNT_POOL_EDIT_ACTION = "accountPool.edit";
const ACCOUNT_POOL_REMOVE_ACTION = "accountPool.remove";
const ACCOUNT_POOL_MUTATE_VERSION = 1;
const ACCOUNT_POOL_STRATEGIES = new Set([
  "fixed-first",
  "round-robin",
  "sticky",
  "quota-aware",
  "cost",
  "latency",
  "reliability",
]);
const MAX_ACCOUNT_POOL_ID_LENGTH = 128;
const MAX_ACCOUNT_POOL_MEMBERS = 64;
const MAX_DECISION_TRACES = 128;
const MAX_DECISION_TRACE_EXCLUSIONS = 64;
const MAX_DECISION_TRACE_FACTS_WINDOW_SECONDS = 2_678_400;

const DECISION_TRACE_API_VERSION = "gateway-decision-trace.openusage/v1";
const DECISION_TRACE_KINDS = new Set([
  "route_advice",
  "gateway_execution",
  "pool_selection",
]);
const DECISION_TRACE_OUTCOMES = new Set([
  "yes",
  "no",
  "defer",
  "selected",
  "unavailable",
  "succeeded",
  "failed",
]);
const DECISION_TRACE_PROVIDERS = new Set([
  "anthropic",
  "deepseek",
  "ollama",
  "openai",
  "openrouter",
]);
const DECISION_TRACE_EXCLUSION_REASONS = new Set([
  "cooldown",
  "credential_backend_unavailable",
  "cross_model_unconfirmed",
  "cross_provider_unconfirmed",
  "cross_region_unconfirmed",
  "disabled",
  "health_unknown",
  "metric_unknown",
  "quota_unknown",
  "unhealthy",
]);
const DECISION_TRACE_FALLBACK_ACTIONS = new Set([
  "none",
  "retry",
  "fail",
  "degrade_to_cheap",
]);

const WINDOWS_ACL_SCRIPT = String.raw`
& {
  $ErrorActionPreference = 'Stop'
  $TokenPath = [Console]::In.ReadToEnd()
  $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
  if ($null -eq $identity.User) { exit 20 }
  $currentSid = $identity.User.Value
  $localSystemSid = 'S-1-5-18'
  $sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor [System.Security.AccessControl.AccessControlSections]::Access
  $acl = [System.IO.File]::GetAccessControl($TokenPath, $sections)
  $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
  if (-not $owner.Equals($identity.User)) { exit 21 }
  if (-not $acl.AreAccessRulesProtected) { exit 22 }
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 2) { exit 23 }
  $seenCurrent = $false
  $seenLocalSystem = $false
  foreach ($rule in $rules) {
    if ($rule.IsInherited) { exit 24 }
    if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { exit 25 }
    if ($rule.FileSystemRights -ne [System.Security.AccessControl.FileSystemRights]::FullControl) { exit 26 }
    if ($rule.InheritanceFlags -ne [System.Security.AccessControl.InheritanceFlags]::None) { exit 27 }
    if ($rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) { exit 28 }
    $ruleSid = $rule.IdentityReference.Value
    if ($ruleSid -eq $currentSid) {
      if ($seenCurrent) { exit 29 }
      $seenCurrent = $true
      continue
    }
    if ($ruleSid -eq $localSystemSid) {
      if ($seenLocalSystem) { exit 30 }
      $seenLocalSystem = $true
      continue
    }
    exit 31
  }
  if (-not $seenCurrent -or -not $seenLocalSystem) { exit 32 }
  [Console]::Out.Write('OK')
}`.trim();

const LOCAL_API_QUERY_FIELDS = new Map([
  ["/v1/health", new Set()],
  ["/v1/schema", new Set()],
  ["/v1/schema.json", new Set()],
  ["/v1/summary", new Set(["today"])],
  ["/v1/snapshot", new Set(["today"])],
  ["/v1/capabilities", new Set()],
  ["/v1/providers", new Set(["providerIds"])],
  ["/v1/capacity", new Set(["limit"])],
  ["/v1/activity/daily", new Set(["from", "to", "providerIds", "modelIds"])],
  ["/v1/balances", new Set(["limit"])],
  ["/v1/costs/daily", new Set(["from", "to", "providerIds", "currencies"])],
  ["/v1/quotas/history", new Set(["providerId", "accountRef", "from", "to", "limit"])],
  ["/v1/sources/status", new Set()],
  ["/v1/changes", new Set(["after", "limit"])],
  ["/v1/quick-connect", new Set()],
]);
const FORBIDDEN_RENDERER_HEADERS = new Set([
  "authorization",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  "x-api-key",
  "x-auth-token",
  "content-length",
  "transfer-encoding",
  "expect",
]);

function discoverPrivateRuntime({ platform, homeDir, localAppData }) {
  try {
    if (platform === "win32") {
      const root = validatedRuntimeRoot(localAppData, path.win32, true);
      const stateDir = path.win32.join(root, "openusage-bar");
      return {
        localApi: {
          transport: "tcp",
          host: LOOPBACK_HOST,
          port: LOCAL_API_PORT,
          tokenPath: path.win32.join(stateDir, "api.token"),
        },
        gateway: {
          transport: "tcp",
          host: LOOPBACK_HOST,
          port: GATEWAY_PORT,
          tokenPath: path.win32.join(stateDir, "gateway.token"),
        },
      };
    }
    if (
      platform === "darwin" ||
      (typeof platform === "string" && platform.startsWith("linux"))
    ) {
      const root = validatedRuntimeRoot(homeDir, path.posix, false);
      const stateDir = path.posix.join(
        root,
        ".local",
        "state",
        "openusage-bar",
      );
      return {
        localApi: {
          transport: "unix",
          socketPath: path.posix.join(stateDir, "openusage.sock"),
        },
        gateway: {
          transport: "tcp",
          host: LOOPBACK_HOST,
          port: GATEWAY_PORT,
          tokenPath: path.posix.join(stateDir, "gateway.token"),
        },
      };
    }
  } catch {
    // Collapse every path/parser failure into one path-free boundary result.
  }
  throw new Error("private runtime unavailable");
}

function validatedRuntimeRoot(value, pathApi, rejectUnc) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 4096 ||
    /[\u0000-\u001f\u007f]/u.test(value) ||
    !pathApi.isAbsolute(value) ||
    value.split(/[\\/]/u).includes("..") ||
    (rejectUnc && value.startsWith("\\\\"))
  ) {
    throw new Error("invalid runtime root");
  }
  const parsed = pathApi.parse(value);
  if (pathApi.normalize(value) === parsed.root) {
    throw new Error("invalid runtime root");
  }
  return pathApi.normalize(value);
}

function classifyRendererRequest({ method, target, headers = {} }) {
  if (
    typeof method !== "string" ||
    typeof target !== "string" ||
    target.length === 0 ||
    target.length > MAX_TARGET_LENGTH ||
    /[\u0000-\u001f\u007f\\#]/u.test(target) ||
    /%(?![0-9A-Fa-f]{2})/u.test(target) ||
    !target.startsWith("/") ||
    target.startsWith("//")
  ) {
    return null;
  }
  if (target === "/gateway/v1/should-send") {
    if (method !== "POST") return null;
    const normalizedHeaders = normalizeRendererHeaders(headers, {
      allowContentLength: true,
    });
    if (
      normalizedHeaders === null ||
      normalizedHeaders["content-type"] !== "application/json" ||
      !isBoundedShouldSendContentLength(normalizedHeaders["content-length"])
    ) {
      return null;
    }
    return {
      service: "gateway",
      method,
      target,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
    };
  }
  if (target === "/host/v1/actions") {
    if (method !== "POST") return null;
    const hostHeaders = normalizeRendererHeaders(headers, {
      allowContentLength: true,
    });
    if (
      hostHeaders === null ||
      hostHeaders["content-type"] !== "application/json" ||
      !isBoundedContentLength(
        hostHeaders["content-length"],
        MAX_HOST_ACTION_REQUEST_BYTES,
      )
    ) {
      return null;
    }
    return {
      service: "host",
      method,
      target,
      headers: { "Content-Type": "application/json" },
    };
  }
  if (target === "/host/v2/gateway-account-operations") {
    if (method !== "POST") return null;
    const hostHeaders = normalizeRendererHeaders(headers, {
      allowContentLength: true,
    });
    if (
      hostHeaders === null ||
      hostHeaders["content-type"] !== "application/json" ||
      !isBoundedContentLength(
        hostHeaders["content-length"],
        MAX_HOST_ACTION_REQUEST_BYTES,
      )
    ) {
      return null;
    }
    return {
      service: "host",
      method,
      target,
      headers: { "Content-Type": "application/json" },
    };
  }

  const normalizedHeaders = normalizeRendererHeaders(headers);
  if (normalizedHeaders === null) return null;

  if (target === "/gateway/v1/health") {
    if (method !== "GET") return null;
    return {
      service: "gateway",
      method,
      target,
      headers: { Accept: RUNTIME_CAPABILITY_MEDIA_TYPE },
    };
  }
  if (target === "/gateway/v1/account-pools") {
    if (method !== "GET") return null;
    return {
      service: "gateway",
      method,
      target,
      headers: { Accept: "application/json" },
    };
  }
  if (target === "/gateway/v1/decision-traces") {
    if (method !== "GET") return null;
    return {
      service: "gateway",
      method,
      target,
      headers: { Accept: "application/json" },
    };
  }
  if (target === "/host/v1/capabilities") {
    if (method !== "GET") return null;
    return {
      service: "host",
      method,
      target,
      headers: {},
    };
  }
  if (target === "/host/v2/gateway-account-capabilities") {
    if (method !== "GET") return null;
    return {
      service: "host",
      method,
      target,
      headers: {},
    };
  }
  const gatewayAccountOperationPoll = target.match(
    /^\/host\/v2\/gateway-account-operations\/(op_[0-9a-f]{32})$/u,
  );
  if (gatewayAccountOperationPoll !== null) {
    if (method !== "GET") return null;
    return {
      service: "host",
      method,
      target,
      operationId: gatewayAccountOperationPoll[1],
      headers: {},
    };
  }
  if (method !== "GET" && method !== "HEAD") return null;

  const question = target.indexOf("?");
  const route = question === -1 ? target : target.slice(0, question);
  if (route.includes("%")) return null;
  const allowedFields = LOCAL_API_QUERY_FIELDS.get(route);
  if (allowedFields === undefined) return null;
  const query = question === -1 ? "" : target.slice(question + 1);
  const seen = new Set();
  for (const [name] of new URLSearchParams(query)) {
    if (!allowedFields.has(name) || seen.has(name)) return null;
    seen.add(name);
  }

  const forwarded = { Accept: "application/json" };
  const validator = normalizedHeaders["if-none-match"];
  if (validator !== undefined) {
    if (
      typeof validator !== "string" ||
      validator.length === 0 ||
      validator.length > MAX_VALIDATOR_LENGTH ||
      /[\r\n\u0000\u007f]/u.test(validator)
    ) {
      return null;
    }
    forwarded["If-None-Match"] = validator;
  }
  return {
    service: "localApi",
    method,
    target,
    headers: forwarded,
  };
}

function normalizeRendererHeaders(
  headers,
  { allowContentLength = false } = {},
) {
  if (headers === null || typeof headers !== "object" || Array.isArray(headers)) {
    return null;
  }
  let headerBytes = 0;
  let headerCount = 0;
  const normalized = Object.create(null);
  for (const [rawName, value] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    headerCount += 1;
    if (typeof value !== "string") return null;
    headerBytes += Buffer.byteLength(rawName, "utf8");
    headerBytes += Buffer.byteLength(value, "utf8");
    if (
      !name ||
      (FORBIDDEN_RENDERER_HEADERS.has(name) &&
        !(allowContentLength && name === "content-length")) ||
      name === "content-encoding" ||
      name.startsWith("x-forwarded-") ||
      name === "forwarded" ||
      Object.prototype.hasOwnProperty.call(normalized, name) ||
      /[\r\n\u0000\u007f]/u.test(rawName) ||
      /[\r\n\u0000\u007f]/u.test(value) ||
      headerCount > MAX_RESPONSE_HEADER_COUNT ||
      headerBytes > MAX_RESPONSE_HEADER_BYTES
    ) {
      return null;
    }
    normalized[name] = value;
  }
  return normalized;
}

function isBoundedShouldSendContentLength(value) {
  return isBoundedContentLength(value, MAX_SHOULD_SEND_REQUEST_BYTES);
}

function isBoundedContentLength(value, maximumBytes) {
  if (typeof value !== "string" || !/^[1-9][0-9]{0,3}$/u.test(value)) {
    return false;
  }
  const length = Number(value);
  return length >= 1 && length <= maximumBytes;
}

function readPrivateToken(
  tokenPath,
  {
    platform = process.platform,
    verifyWindowsAcl,
    realpath = fs.realpathSync.native,
    resolvePath =
      platform === "win32" ? fs.realpathSync.native : path.resolve,
  } = {},
) {
  const unavailable = () => new Error("private token unavailable");
  let descriptor;
  try {
    if (
      typeof tokenPath !== "string" ||
      !path.isAbsolute(tokenPath) ||
      tokenPath.length > 4096 ||
      /[\u0000-\u001f\u007f]/u.test(tokenPath) ||
      typeof realpath !== "function" ||
      typeof resolvePath !== "function"
    ) {
      throw unavailable();
    }
    const parentPath = path.dirname(tokenPath);
    const parent = fs.lstatSync(parentPath);
    if (
      !parent.isDirectory() ||
      parent.isSymbolicLink() ||
      !privateParentPathMatches(
        realpath(parentPath),
        resolvePath(parentPath),
        platform,
      )
    ) {
      throw unavailable();
    }
    const existing = fs.lstatSync(tokenPath);
    if (
      !existing.isFile() ||
      existing.isSymbolicLink() ||
      existing.nlink !== 1
    ) {
      throw unavailable();
    }
    if (platform === "win32") {
      if (
        typeof verifyWindowsAcl !== "function" ||
        verifyWindowsAcl(tokenPath, existing) !== true
      ) {
        throw unavailable();
      }
    } else {
      const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
      if (
        currentUid === null ||
        parent.uid !== currentUid ||
        (parent.mode & 0o777) !== 0o700 ||
        existing.uid !== currentUid ||
        (existing.mode & 0o777) !== 0o600
      ) {
        throw unavailable();
      }
    }

    let flags = fs.constants.O_RDONLY;
    if (
      process.platform !== "win32" &&
      typeof fs.constants.O_NOFOLLOW === "number"
    ) {
      flags |= fs.constants.O_NOFOLLOW;
    }
    if (typeof fs.constants.O_CLOEXEC === "number") {
      flags |= fs.constants.O_CLOEXEC;
    }
    descriptor = fs.openSync(tokenPath, flags);
    const opened = fs.fstatSync(descriptor);
    if (
      !opened.isFile() ||
      opened.nlink !== 1 ||
      opened.dev !== existing.dev ||
      opened.ino !== existing.ino ||
      (platform !== "win32" &&
        (opened.uid !== process.getuid() || (opened.mode & 0o777) !== 0o600))
    ) {
      throw unavailable();
    }
    const raw = Buffer.alloc(257);
    const length = fs.readSync(descriptor, raw, 0, raw.length, null);
    const after = fs.fstatSync(descriptor);
    const current = fs.lstatSync(tokenPath);
    if (
      after.dev !== opened.dev ||
      after.ino !== opened.ino ||
      current.dev !== opened.dev ||
      current.ino !== opened.ino ||
      current.nlink !== 1
    ) {
      throw unavailable();
    }
    const tokenBytes = raw.subarray(0, length);
    const token = tokenBytes.toString("ascii");
    if (
      length < 43 ||
      length > 256 ||
      !tokenBytes.every((byte) => byte >= 0x21 && byte <= 0x7e) ||
      token.length !== length ||
      !/^[\x21-\x7e]+$/u.test(token)
    ) {
      throw unavailable();
    }
    fs.closeSync(descriptor);
    descriptor = undefined;
    return token;
  } catch {
    throw unavailable();
  } finally {
    if (descriptor !== undefined) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // The public result remains a stable fail-closed error below.
      }
    }
  }
}

function normalizeLocalWindowsPath(value, { rejectDriveRoot = false } = {}) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 4096 ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    return null;
  }
  let localPath = value;
  if (localPath.startsWith("\\\\?\\")) {
    localPath = localPath.slice(4);
    if (!/^[A-Za-z]:[\\/]/u.test(localPath)) return null;
  }
  if (
    !/^[A-Za-z]:[\\/]/u.test(localPath) ||
    localPath.startsWith("\\\\") ||
    localPath.slice(2).includes(":") ||
    localPath.split(/[\\/]/u).includes("..") ||
    !path.win32.isAbsolute(localPath)
  ) {
    return null;
  }
  const normalized = path.win32.normalize(localPath);
  if (
    !/^[A-Za-z]:\\/u.test(normalized) ||
    (rejectDriveRoot && normalized === path.win32.parse(normalized).root)
  ) {
    return null;
  }
  return normalized;
}

function canonicalWindowsPathMatches(resolvedPath, expectedPath) {
  const normalizedResolved = normalizeLocalWindowsPath(resolvedPath);
  const normalizedExpected = normalizeLocalWindowsPath(expectedPath);
  return (
    normalizedResolved !== null &&
    normalizedExpected !== null &&
    normalizedResolved.toLowerCase() === normalizedExpected.toLowerCase()
  );
}

function privateParentPathMatches(resolvedPath, expectedPath, platform) {
  if (typeof resolvedPath !== "string" || typeof expectedPath !== "string") {
    return false;
  }
  if (platform !== "win32") return resolvedPath === expectedPath;
  const normalizedResolved = normalizeLocalWindowsPath(resolvedPath);
  const normalizedExpected = normalizeLocalWindowsPath(expectedPath);
  if (normalizedResolved !== null || normalizedExpected !== null) {
    return (
      normalizedResolved !== null &&
      normalizedExpected !== null &&
      normalizedResolved.toLowerCase() === normalizedExpected.toLowerCase()
    );
  }
  return process.platform !== "win32" && resolvedPath === expectedPath;
}

function resolveTrustedWindowsPowerShell({
  systemRoot,
  processArch,
  pathExists,
  realpath,
}) {
  const normalizedRoot = normalizeLocalWindowsPath(systemRoot, {
    rejectDriveRoot: true,
  });
  if (
    normalizedRoot === null ||
    typeof pathExists !== "function" ||
    typeof realpath !== "function" ||
    pathExists(normalizedRoot) !== true ||
    !canonicalWindowsPathMatches(realpath(normalizedRoot), normalizedRoot)
  ) {
    return null;
  }

  let systemDirectories;
  if (processArch === "ia32") {
    systemDirectories = ["Sysnative", "System32"];
  } else if (processArch === "x64" || processArch === "arm64") {
    systemDirectories = ["System32"];
  } else {
    return null;
  }

  for (const systemDirectory of systemDirectories) {
    const candidate = path.win32.join(
      normalizedRoot,
      systemDirectory,
      "WindowsPowerShell",
      "v1.0",
      "powershell.exe",
    );
    if (
      pathExists(candidate) === true &&
      canonicalWindowsPathMatches(realpath(candidate), candidate)
    ) {
      return candidate;
    }
  }
  return null;
}

function createWindowsAclVerifier({
  systemRoot = process.env.SystemRoot,
  processArch = process.arch,
  pathExists = fs.existsSync,
  realpath = fs.realpathSync.native,
  spawnSync = childProcess.spawnSync,
} = {}) {
  return function verifyWindowsAcl(tokenPath) {
    try {
      if (
        typeof spawnSync !== "function" ||
        normalizeLocalWindowsPath(tokenPath) === null
      ) {
        return false;
      }
      const executable = resolveTrustedWindowsPowerShell({
        systemRoot,
        processArch,
        pathExists,
        realpath,
      });
      if (executable === null) return false;
      const result = spawnSync(
        executable,
        [
          "-NoLogo",
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy",
          "Bypass",
          "-Command",
          WINDOWS_ACL_SCRIPT,
        ],
        {
          encoding: "utf8",
          input: tokenPath,
          maxBuffer: WINDOWS_ACL_MAX_BUFFER,
          shell: false,
          timeout: WINDOWS_ACL_TIMEOUT_MS,
          windowsHide: true,
        },
      );
      return (
        result?.status === 0 &&
        result.signal == null &&
        result.error == null &&
        result.stdout === "OK" &&
        result.stderr === ""
      );
    } catch {
      return false;
    }
  };
}

function observerDaemonArguments(runtime, { offline = false } = {}) {
  const endpoint = runtime?.localApi;
  const common = [
    ...(offline === true ? ["--offline"] : []),
    "daemon",
    "--interval",
    "300",
    "--api-transport",
  ];
  if (
    endpoint?.transport === "unix" &&
    typeof endpoint.socketPath === "string" &&
    path.posix.isAbsolute(endpoint.socketPath) &&
    !/[\u0000-\u001f\u007f]/u.test(endpoint.socketPath)
  ) {
    return [...common, "unix", "--api-socket", endpoint.socketPath];
  }
  if (
    endpoint?.transport === "tcp" &&
    endpoint.host === LOOPBACK_HOST &&
    endpoint.port === LOCAL_API_PORT &&
    typeof endpoint.tokenPath === "string" &&
    (path.isAbsolute(endpoint.tokenPath) || path.win32.isAbsolute(endpoint.tokenPath)) &&
    !/[\u0000-\u001f\u007f]/u.test(endpoint.tokenPath)
  ) {
    return [
      ...common,
      "tcp",
      "--api-port",
      String(LOCAL_API_PORT),
      "--api-token-path",
      endpoint.tokenPath,
    ];
  }
  return null;
}

async function probePrivateObserver(
  runtime,
  { platform = process.platform, verifyWindowsAcl, deadlineMs = 1_500 } = {},
) {
  try {
    const classified = classifyRendererRequest({
      method: "GET",
      target: "/v1/health",
      headers: {},
    });
    const result = await fetchRendererResponse(classified, {
      runtime,
      platform,
      verifyWindowsAcl,
      deadlineMs,
    });
    if (result.statusCode !== 200) return false;
    const localHealthPayload = parseBoundedJson(result.body);
    if (localHealthPayload === null) return false;
    return (
      composeRendererCapability({
        gatewayPayload: null,
        localHealthPayload,
      }).observer.operational === "ready"
    );
  } catch {
    return false;
  }
}

async function fetchPrivateObserverJson(target, options = {}) {
  if (target !== "/v1/snapshot" && target !== "/v1/capacity") {
    return null;
  }
  try {
    const {
      runtime,
      platform = process.platform,
      verifyWindowsAcl,
      deadlineMs = DEFAULT_DEADLINE_MS,
    } = options ?? {};
    const classified = classifyRendererRequest({
      method: "GET",
      target,
      headers: {},
    });
    const result = await privateRequest(
      runtime?.localApi,
      classified,
      MAX_LOCAL_RESPONSE_BYTES,
      { platform, verifyWindowsAcl, deadlineMs },
    );
    if (result.statusCode !== 200) return null;
    const payload = parsePrivateObserverJson(result.body);
    return payload;
  } catch {
    return null;
  }
}

async function startOrProbePrivateObserver({
  runtime,
  platform = process.platform,
  command,
  offline = false,
  verifyWindowsAcl,
  probeObserver = probePrivateObserver,
  spawnProcess = childProcess.spawn,
  wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  attempts = 40,
} = {}) {
  const probe = async () => {
    try {
      return (
        (await probeObserver(runtime, { platform, verifyWindowsAcl })) === true
      );
    } catch {
      return false;
    }
  };
  if (await probe()) return { ready: true, child: null };
  const arguments_ = observerDaemonArguments(runtime, { offline });
  if (
    arguments_ === null ||
    typeof command !== "string" ||
    !path.isAbsolute(command) ||
    command.length > 4096 ||
    /[\u0000-\u001f\u007f]/u.test(command) ||
    typeof spawnProcess !== "function"
  ) {
    return { ready: false, child: null };
  }
  let child;
  try {
    child = spawnProcess(command, arguments_, {
      shell: false,
      stdio: "ignore",
      windowsHide: true,
    });
  } catch {
    return { ready: false, child: null };
  }
  let childFailed = false;
  if (child && typeof child.once === "function") {
    child.once("error", () => {
      childFailed = true;
    });
  }
  const boundedAttempts =
    Number.isInteger(attempts) && attempts >= 1 && attempts <= 40 ? attempts : 40;
  for (let attempt = 0; attempt < boundedAttempts; attempt += 1) {
    try {
      await wait(250);
    } catch {
      return { ready: false, child };
    }
    if (childFailed) return { ready: false, child };
    if (await probe()) return { ready: true, child };
  }
  return { ready: false, child };
}

async function startOrProbeLegacyDashboard({
  command,
  probeDashboard,
  spawnProcess = childProcess.spawn,
  wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  attempts = 40,
} = {}) {
  const probe = async () => {
    try {
      return typeof probeDashboard === "function" &&
        (await probeDashboard()) === true;
    } catch {
      return false;
    }
  };
  if (await probe()) return { ready: true, child: null };
  if (
    typeof command !== "string" ||
    !path.isAbsolute(command) ||
    command.length > 4096 ||
    /[\u0000-\u001f\u007f]/u.test(command) ||
    typeof spawnProcess !== "function" ||
    typeof wait !== "function"
  ) {
    return { ready: false, child: null };
  }

  let child;
  try {
    child = spawnProcess(
      command,
      ["dashboard", "--port", String(LEGACY_DASHBOARD_PORT)],
      { shell: false, stdio: "ignore", windowsHide: true },
    );
  } catch {
    return { ready: false, child: null };
  }
  if (
    child === null ||
    typeof child !== "object" ||
    typeof child.once !== "function" ||
    typeof child.kill !== "function"
  ) {
    terminateChild(child);
    return { ready: false, child: null };
  }

  let childFailed = false;
  child.once("error", () => {
    childFailed = true;
  });
  child.once("exit", () => {
    childFailed = true;
  });
  const boundedAttempts =
    Number.isInteger(attempts) && attempts >= 1 && attempts <= 40 ? attempts : 40;
  for (let attempt = 0; attempt < boundedAttempts; attempt += 1) {
    try {
      await wait(250);
    } catch {
      terminateChild(child);
      return { ready: false, child: null };
    }
    if (childFailed) {
      terminateChild(child);
      return { ready: false, child: null };
    }
    const ready = await probe();
    if (ready && !childFailed) return { ready: true, child };
    if (childFailed) {
      terminateChild(child);
      return { ready: false, child: null };
    }
  }
  terminateChild(child);
  return { ready: false, child: null };
}

function terminateChild(child) {
  try {
    if (child && typeof child.kill === "function") child.kill();
  } catch {
    // The caller still receives a cleared reference after cleanup fails.
  }
}

function validatePrivateUnixSocket(socketPath) {
  try {
    if (
      typeof socketPath !== "string" ||
      !path.isAbsolute(socketPath) ||
      socketPath.length > 4096 ||
      /[\u0000-\u001f\u007f]/u.test(socketPath) ||
      typeof process.getuid !== "function"
    ) {
      return false;
    }
    const uid = process.getuid();
    const parentPath = path.dirname(socketPath);
    const parent = fs.lstatSync(parentPath);
    const socket = fs.lstatSync(socketPath);
    return (
      parent.isDirectory() &&
      !parent.isSymbolicLink() &&
      parent.uid === uid &&
      (parent.mode & 0o777) === 0o700 &&
      fs.realpathSync.native(parentPath) === path.resolve(parentPath) &&
      socket.isSocket() &&
      !socket.isSymbolicLink() &&
      socket.uid === uid &&
      socket.nlink === 1 &&
      (socket.mode & 0o777) === 0o600
    );
  } catch {
    return false;
  }
}

async function fetchRendererResponse(
  classified,
  {
    runtime,
    platform = process.platform,
    verifyWindowsAcl,
    deadlineMs = DEFAULT_DEADLINE_MS,
    hostActionExecutor,
    providerConfigExecutor,
    gatewayAccountEditorExecutor,
    gatewayAccountOperationHost,
    spawnProcess = childProcess.spawn,
  } = {},
) {
  if (!classified || !runtime) {
    if (classified?.service !== "host") {
      throw new Error("private service unavailable");
    }
  }
  if (
    classified.service === "host" &&
    classified.method === "GET" &&
    classified.operationId !== undefined
  ) {
    const result = gatewayAccountOperationHost?.poll(classified.operationId);
    return result === null || result === undefined
      ? rendererJsonResponse({ error: { code: "not_found" } }, 404)
      : rendererJsonResponse(result);
  }
  if (
    classified.service === "host" &&
    classified.method === "POST" &&
    classified.target === "/host/v2/gateway-account-operations"
  ) {
    const result = await gatewayAccountOperationHost?.open(
      classified.gatewayAccountIntent,
    );
    if (result?.kind === "busy") {
      return rendererJsonResponse({ error: { code: "helper_busy" } }, 409);
    }
    if (result?.kind !== "opened") throw requestFailure("unavailable");
    return rendererJsonResponse(result.response, 202);
  }
  if (
    classified.service === "host" &&
    classified.method === "GET" &&
    classified.target === "/host/v2/gateway-account-capabilities"
  ) {
    return rendererJsonResponse(
      gatewayAccountOperationCapabilities({
        gatewayAccountEditorExecutor,
        platform,
      }),
    );
  }
  if (
    classified.service === "host" &&
    classified.method === "GET" &&
    classified.target === "/host/v1/capabilities"
  ) {
    return rendererJsonResponse(
      hostActionCapabilities({ hostActionExecutor, providerConfigExecutor }),
    );
  }
  if (
    classified.service === "host" &&
    classified.method === "POST" &&
    classified.target === "/host/v1/actions"
  ) {
    const result = await executeHostActionCommand(classified.actionRequest, {
      hostActionExecutor,
      providerConfigExecutor,
      spawnProcess,
      deadlineMs,
    });
    if (result === null) throw requestFailure("unavailable");
    return rendererJsonResponse(result);
  }
  const requestOptions = { platform, verifyWindowsAcl, deadlineMs };
  if (
    classified.service === "gateway" &&
    classified.method === "POST" &&
    classified.target === "/gateway/v1/should-send"
  ) {
    const gatewayResult = await privateRequest(
      runtime.gateway,
      classified,
      MAX_SHOULD_SEND_RESPONSE_BYTES,
      requestOptions,
    );
    if (gatewayResult.statusCode !== 200) {
      throw requestFailure("unavailable");
    }
    const decision = sanitizeShouldSendDecision(gatewayResult.body);
    if (decision === null) throw requestFailure("invalid");
    const body = Buffer.from(JSON.stringify(decision), "utf8");
    return {
      statusCode: 200,
      headers: {
        "Cache-Control": "no-store",
        "Content-Length": String(body.length),
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      body,
    };
  }
  if (
    classified.service === "gateway" &&
    classified.method === "GET" &&
    classified.target === "/gateway/v1/account-pools"
  ) {
    const gatewayResult = await privateRequest(
      runtime.gateway,
      classified,
      MAX_ACCOUNT_POOLS_RESPONSE_BYTES,
      requestOptions,
    );
    if (gatewayResult.statusCode !== 200) {
      throw requestFailure("unavailable");
    }
    const accountPools = sanitizeAccountPoolsPayload(gatewayResult.body);
    if (accountPools === null) throw requestFailure("invalid");
    const body = Buffer.from(JSON.stringify(accountPools), "utf8");
    return {
      statusCode: 200,
      headers: {
        "Cache-Control": "no-store",
        "Content-Length": String(body.length),
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      body,
    };
  }
  if (
    classified.service === "gateway" &&
    classified.method === "GET" &&
    classified.target === "/gateway/v1/decision-traces"
  ) {
    const gatewayResult = await privateRequest(
      runtime.gateway,
      classified,
      MAX_DECISION_TRACES_RESPONSE_BYTES,
      requestOptions,
    );
    if (gatewayResult.statusCode !== 200) {
      throw requestFailure("unavailable");
    }
    const decisionTraces = sanitizeDecisionTracesPayload(gatewayResult.body);
    if (decisionTraces === null) throw requestFailure("invalid");
    return rendererJsonResponse(decisionTraces);
  }
  if (classified.service === "localApi") {
    return privateRequest(
      runtime.localApi,
      classified,
      MAX_LOCAL_RESPONSE_BYTES,
      requestOptions,
    );
  }
  if (classified.service !== "gateway") {
    throw new Error("private service unavailable");
  }

  const healthRequest = classifyRendererRequest({
    method: "GET",
    target: "/v1/health",
    headers: {},
  });
  const [gatewayResult, localHealthResult] = await Promise.allSettled([
    privateRequest(
      runtime.gateway,
      classified,
      MAX_CAPABILITY_RESPONSE_BYTES,
      requestOptions,
    ),
    privateRequest(
      runtime.localApi,
      healthRequest,
      MAX_CAPABILITY_RESPONSE_BYTES,
      requestOptions,
    ),
  ]);

  let gatewayPayload = null;
  let gatewayFailure = null;
  if (gatewayResult.status === "fulfilled") {
    if (gatewayResult.value.statusCode === 200) {
      gatewayPayload = parseBoundedJson(gatewayResult.value.body);
      if (gatewayPayload === null) gatewayFailure = "capability_invalid";
    } else {
      gatewayFailure = "gateway_unavailable";
    }
  } else {
    gatewayFailure = failureCode(gatewayResult.reason);
  }
  let localHealthPayload = null;
  if (
    localHealthResult.status === "fulfilled" &&
    localHealthResult.value.statusCode === 200
  ) {
    localHealthPayload = parseBoundedJson(localHealthResult.value.body);
  }
  const capability = composeRendererCapability({
    gatewayPayload,
    localHealthPayload,
    gatewayFailure,
  });
  const body = Buffer.from(JSON.stringify(capability), "utf8");
  return {
    statusCode: 200,
    headers: {
      "Cache-Control": "no-store",
      "Content-Length": String(body.length),
      "Content-Type": "application/json; charset=utf-8",
      ...(gatewayResult.status === "fulfilled" &&
      gatewayResult.value.headers.Vary === "Accept"
        ? { Vary: "Accept" }
        : {}),
      "X-Content-Type-Options": "nosniff",
    },
    body,
  };
}

async function privateRequest(
  endpoint,
  classified,
  maxBodyBytes,
  { platform, verifyWindowsAcl, deadlineMs },
) {
  const headers = { ...classified.headers };
  let requestBody;
  if (
    classified.method === "POST" &&
    classified.target === "/gateway/v1/should-send" &&
    Buffer.isBuffer(classified.body) &&
    classified.body.length >= 1 &&
    classified.body.length <= MAX_SHOULD_SEND_REQUEST_BYTES
  ) {
    requestBody = classified.body;
    headers["Content-Length"] = String(requestBody.length);
  } else if (classified.body !== undefined) {
    throw requestFailure("invalid");
  } else if (
    classified.method === "POST" &&
    classified.target === "/gateway/v1/should-send"
  ) {
    throw requestFailure("invalid");
  }
  const options = {
    method: classified.method,
    path: classified.target,
    agent: false,
    maxHeaderSize: MAX_RESPONSE_HEADER_BYTES,
  };
  if (endpoint?.transport === "unix") {
    if (
      requestBody !== undefined ||
      !validatePrivateUnixSocket(endpoint.socketPath)
    ) {
      throw requestFailure("unavailable");
    }
    options.socketPath = endpoint.socketPath;
    headers.Host = "localhost";
  } else if (
    endpoint?.transport === "tcp" &&
    endpoint.host === LOOPBACK_HOST &&
    Number.isInteger(endpoint.port) &&
    endpoint.port >= 1 &&
    endpoint.port <= 65535 &&
    typeof endpoint.tokenPath === "string"
  ) {
    const token = readPrivateToken(endpoint.tokenPath, {
      platform,
      verifyWindowsAcl,
    });
    options.hostname = LOOPBACK_HOST;
    options.port = endpoint.port;
    headers.Host = `${LOOPBACK_HOST}:${endpoint.port}`;
    headers.Authorization = `Bearer ${token}`;
  } else {
    throw requestFailure("unavailable");
  }
  options.headers = headers;
  return boundedHttpRequest(options, maxBodyBytes, deadlineMs, requestBody);
}

function boundedHttpRequest(options, maxBodyBytes, deadlineMs, requestBody) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timedOut = false;
    let deadline;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      callback(value);
    };
    const request = http.request(options, (response) => {
      if (!headersAreBounded(response.rawHeaders)) {
        finish(reject, requestFailure("invalid"));
        response.destroy();
        return;
      }
      const chunks = [];
      let total = 0;
      response.on("data", (chunk) => {
        total += chunk.length;
        if (total > maxBodyBytes) {
          finish(reject, requestFailure("invalid"));
          response.destroy();
          request.destroy();
          return;
        }
        chunks.push(chunk);
      });
      response.on("end", () => {
        const body = Buffer.concat(chunks, total);
        finish(resolve, {
          statusCode:
            Number.isInteger(response.statusCode) &&
            response.statusCode >= 100 &&
            response.statusCode <= 599
              ? response.statusCode
              : 502,
          headers: sanitizeResponseHeaders(
            response.headers,
            body.length,
            options.method,
            maxBodyBytes,
          ),
          body,
        });
      });
      response.on("aborted", () => finish(reject, requestFailure("unavailable")));
      response.on("error", (error) =>
        finish(reject, requestFailure(requestErrorKind(error, false))),
      );
    });
    deadline = setTimeout(() => {
      timedOut = true;
      finish(reject, requestFailure("timeout"));
      request.destroy();
    }, validDeadline(deadlineMs));
    request.on("error", (error) => {
      finish(reject, requestFailure(requestErrorKind(error, timedOut)));
    });
    request.end(requestBody);
  });
}

function validDeadline(value) {
  return Number.isInteger(value) && value >= 50 && value <= 30_000
    ? value
    : DEFAULT_DEADLINE_MS;
}

function headersAreBounded(rawHeaders) {
  if (!Array.isArray(rawHeaders) || rawHeaders.length / 2 > MAX_RESPONSE_HEADER_COUNT) {
    return false;
  }
  let bytes = 0;
  for (const item of rawHeaders) {
    if (typeof item !== "string" || /[\r\n\u0000\u007f]/u.test(item)) return false;
    bytes += Buffer.byteLength(item, "utf8");
    if (bytes > MAX_RESPONSE_HEADER_BYTES) return false;
  }
  return true;
}

function sanitizeResponseHeaders(headers, bodyLength, method, maxBodyBytes) {
  let contentLength = bodyLength;
  const representationLength = singleHeader(headers["content-length"]);
  if (method === "HEAD" && /^[0-9]{1,10}$/u.test(representationLength ?? "")) {
    const parsedLength = Number(representationLength);
    if (
      Number.isSafeInteger(parsedLength) &&
      parsedLength >= 0 &&
      parsedLength <= maxBodyBytes
    ) {
      contentLength = parsedLength;
    }
  }
  const result = {
    "Cache-Control": safeCacheControl(headers["cache-control"]),
    "Content-Length": String(contentLength),
    "Content-Type": safeContentType(headers["content-type"]),
  };
  const etag = singleHeader(headers.etag);
  if (etag !== null && /^(?:W\/)?"[\x21\x23-\x7e]{1,128}"$/u.test(etag)) {
    result.ETag = etag;
  }
  const retryAfter = singleHeader(headers["retry-after"]);
  if (retryAfter !== null && /^[0-9]{1,5}$/u.test(retryAfter)) {
    result["Retry-After"] = retryAfter;
  }
  if (singleHeader(headers.vary) === "Accept") result.Vary = "Accept";
  result["X-Content-Type-Options"] = "nosniff";
  return result;
}

function safeCacheControl(value) {
  const normalized = singleHeader(value);
  return normalized === "private, no-cache" ? normalized : "no-store";
}

function safeContentType(value) {
  const normalized = singleHeader(value);
  return normalized === "application/json" ||
    normalized === "application/json; charset=utf-8"
    ? normalized
    : "application/json; charset=utf-8";
}

function singleHeader(value) {
  return typeof value === "string" && value.length <= 256 ? value : null;
}

function parseBoundedJson(body) {
  if (!Buffer.isBuffer(body) || body.length > MAX_CAPABILITY_RESPONSE_BYTES) {
    return null;
  }
  try {
    return JSON.parse(body.toString("utf8"));
  } catch {
    return null;
  }
}

function canonicalShouldSendRequestBody(body) {
  const topLevelLexemes = new Map();
  const value = parseStrictJsonObject(body, MAX_SHOULD_SEND_REQUEST_BYTES, {
    topLevelLexemes,
  });
  if (value === null || !hasExactOwnKeys(value, [
    "provider",
    "model",
    "estimated_tokens",
    "window",
  ])) {
    return null;
  }
  const provider = boundedShouldSendText(
    value.provider,
    MAX_SHOULD_SEND_PROVIDER_LENGTH,
  );
  const model = boundedShouldSendText(
    value.model,
    MAX_SHOULD_SEND_MODEL_LENGTH,
  );
  const window = boundedShouldSendText(
    value.window,
    MAX_SHOULD_SEND_WINDOW_LENGTH,
  );
  if (
    provider === null ||
    model === null ||
    window === null ||
    !/^[1-9][0-9]*$/u.test(topLevelLexemes.get("estimated_tokens") ?? "") ||
    !Number.isInteger(value.estimated_tokens) ||
    value.estimated_tokens < 1 ||
    value.estimated_tokens > MAX_ESTIMATED_TOKENS
  ) {
    return null;
  }
  const canonical = Buffer.from(JSON.stringify({
    provider,
    model,
    estimated_tokens: value.estimated_tokens,
    window,
  }), "utf8");
  return canonical.length <= MAX_SHOULD_SEND_REQUEST_BYTES ? canonical : null;
}

function sanitizeShouldSendDecision(body) {
  const value = parseStrictJsonObject(body, MAX_SHOULD_SEND_RESPONSE_BYTES);
  if (
    value === null ||
    !hasRequiredOwnKeys(value, [
      "decision",
      "confidence",
      "reason",
      "details",
    ])
  ) {
    return null;
  }
  if (
    !SHOULD_SEND_DECISIONS.has(value.decision) ||
    typeof value.confidence !== "number" ||
    !Number.isFinite(value.confidence) ||
    value.confidence < 0 ||
    value.confidence > 1 ||
    !SHOULD_SEND_REASONS.has(value.reason) ||
    !isJsonRecord(value.details) ||
    !hasRequiredOwnKeys(value.details, [
      "quota_remaining",
      "burn_rate_per_min",
      "predicted_exhaustion_minutes",
    ])
  ) {
    return null;
  }
  const quotaRemaining = nullableNonNegativeNumber(
    value.details.quota_remaining,
  );
  const burnRatePerMinute = nullableNonNegativeNumber(
    value.details.burn_rate_per_min,
  );
  const predictedExhaustionMinutes = nullableNonNegativeNumber(
    value.details.predicted_exhaustion_minutes,
  );
  if (
    quotaRemaining === undefined ||
    burnRatePerMinute === undefined ||
    predictedExhaustionMinutes === undefined
  ) {
    return null;
  }
  const details = {
    quota_remaining: quotaRemaining,
    burn_rate_per_min: burnRatePerMinute,
    predicted_exhaustion_minutes: predictedExhaustionMinutes,
  };
  if (Object.prototype.hasOwnProperty.call(value.details, "limit_per_window")) {
    const limitPerWindow = nullableNonNegativeNumber(
      value.details.limit_per_window,
    );
    if (limitPerWindow === undefined) return null;
    details.limit_per_window = limitPerWindow;
  }
  const deferUntil =
    value.decision === "defer" &&
    Object.prototype.hasOwnProperty.call(value, "defer_until") &&
    isValidShouldSendTimestamp(value.defer_until)
      ? value.defer_until
      : null;
  return {
    decision: value.decision,
    confidence: value.confidence,
    reason: value.reason,
    defer_until: deferUntil,
    details,
  };
}

function sanitizeAccountPoolsPayload(body) {
  const value = parseStrictJsonObject(body, MAX_ACCOUNT_POOLS_RESPONSE_BYTES);
  if (
    value === null ||
    !hasExactOwnKeys(value, ["accounts", "pools"]) ||
    !Array.isArray(value.accounts) ||
    value.accounts.length > 256 ||
    !Array.isArray(value.pools) ||
    value.pools.length > 64
  ) {
    return null;
  }
  const accounts = [];
  const displayIds = new Set();
  for (const item of value.accounts) {
    const account = sanitizeAccountPoolAccount(item);
    if (account === null || displayIds.has(account.displayId)) return null;
    displayIds.add(account.displayId);
    accounts.push(account);
  }
  const pools = [];
  const poolIds = new Set();
  for (const item of value.pools) {
    const pool = sanitizeAccountPoolPublicPool(item);
    if (pool === null || poolIds.has(pool.poolId)) return null;
    poolIds.add(pool.poolId);
    pools.push(pool);
  }
  return { accounts, pools };
}

function sanitizeDecisionTracesPayload(body) {
  if (!isBoundedDecisionTraceBuffer(
    body,
    MAX_DECISION_TRACES_RESPONSE_BYTES,
  )) return null;
  const value = parseStrictJsonObject(
    body,
    MAX_DECISION_TRACES_RESPONSE_BYTES,
  );
  return sanitizeDecisionTracesValue(value);
}

function sanitizeDecisionTracesValue(value) {
  const root = ownDataRecord(value, ["apiVersion", "traces"]);
  if (
    root === null ||
    root.apiVersion !== DECISION_TRACE_API_VERSION
  ) {
    return null;
  }
  const items = ownDataArray(root.traces, MAX_DECISION_TRACES);
  if (items === null) return null;

  const traces = [];
  const traceIds = new Set();
  let previousOccurredAt = null;
  for (const item of items) {
    const trace = sanitizeDecisionTrace(item);
    if (
      trace === null ||
      traceIds.has(trace.traceId) ||
      (previousOccurredAt !== null &&
        previousOccurredAt < trace.occurredAt)
    ) {
      return null;
    }
    traceIds.add(trace.traceId);
    previousOccurredAt = trace.occurredAt;
    traces.push(trace);
  }
  return { apiVersion: DECISION_TRACE_API_VERSION, traces };
}

function isBoundedDecisionTraceBuffer(value, maximumBytes) {
  try {
    return (
      Buffer.isBuffer(value) &&
      Object.getPrototypeOf(value) === Buffer.prototype &&
      Number.isInteger(value.length) &&
      value.length >= 1 &&
      value.length <= maximumBytes
    );
  } catch {
    return false;
  }
}

function sanitizeDecisionTrace(value) {
  const trace = ownDataRecord(value, [
    "traceId",
    "occurredAt",
    "kind",
    "execution",
    "outcome",
    "reason",
    "pool",
    "selected",
    "exclusions",
    "fallback",
    "factsWindow",
  ]);
  if (
    trace === null ||
    typeof trace.traceId !== "string" ||
    !/^trace_[0-9a-f]{32}$/u.test(trace.traceId) ||
    !isCanonicalDecisionTraceTimestamp(trace.occurredAt) ||
    !DECISION_TRACE_KINDS.has(trace.kind) ||
    !DECISION_TRACE_OUTCOMES.has(trace.outcome)
  ) {
    return null;
  }

  const pool = sanitizeDecisionTracePool(trace.pool);
  const selected = sanitizeDecisionTraceSelected(trace.selected);
  const exclusions = sanitizeDecisionTraceExclusions(trace.exclusions);
  const fallback = sanitizeDecisionTraceFallback(trace.fallback);
  const factsWindow = sanitizeDecisionTraceFactsWindow(trace.factsWindow);
  if (
    pool === undefined ||
    selected === undefined ||
    exclusions === null ||
    fallback === undefined ||
    factsWindow === undefined
  ) {
    return null;
  }

  if (trace.kind === "route_advice") {
    if (
      trace.execution !== "advice_only" ||
      !SHOULD_SEND_DECISIONS.has(trace.outcome) ||
      !SHOULD_SEND_REASONS.has(trace.reason) ||
      pool !== null ||
      selected !== null ||
      exclusions.length !== 0 ||
      fallback !== null
    ) {
      return null;
    }
  } else if (trace.kind === "gateway_execution") {
    if (
      trace.execution !== "executed" ||
      (trace.outcome !== "succeeded" && trace.outcome !== "failed") ||
      trace.reason !== null ||
      pool !== null ||
      selected === null ||
      selected.providerId === null ||
      selected.accountDisplayId !== null ||
      exclusions.length !== 0 ||
      fallback === null ||
      factsWindow !== null
    ) {
      return null;
    }
  } else if (
    trace.execution !== "executed" ||
    (trace.outcome !== "selected" && trace.outcome !== "unavailable") ||
    trace.reason !== null ||
    pool === null ||
    fallback !== null ||
    factsWindow !== null ||
    (trace.outcome === "selected" &&
      (selected === null ||
        selected.providerId === null ||
        selected.accountDisplayId === null)) ||
    (trace.outcome === "unavailable" && selected !== null)
  ) {
    return null;
  }

  return {
    traceId: trace.traceId,
    occurredAt: trace.occurredAt,
    kind: trace.kind,
    execution: trace.execution,
    outcome: trace.outcome,
    reason: trace.reason,
    pool,
    selected,
    exclusions,
    fallback,
    factsWindow,
  };
}

function sanitizeDecisionTracePool(value) {
  if (value === null) return null;
  const pool = ownDataRecord(value, ["poolId", "revision", "strategy"]);
  if (
    pool === null ||
    !isPublicPoolId(pool.poolId) ||
    !Number.isSafeInteger(pool.revision) ||
    pool.revision < 1 ||
    !ACCOUNT_POOL_STRATEGIES.has(pool.strategy)
  ) {
    return undefined;
  }
  return {
    poolId: pool.poolId,
    revision: pool.revision,
    strategy: pool.strategy,
  };
}

function sanitizeDecisionTraceSelected(value) {
  if (value === null) return null;
  const selected = ownDataRecord(value, ["providerId", "accountDisplayId"]);
  if (
    selected === null ||
    (selected.providerId !== null &&
      !DECISION_TRACE_PROVIDERS.has(selected.providerId)) ||
    (selected.accountDisplayId !== null &&
      (typeof selected.accountDisplayId !== "string" ||
        !/^acct_[0-9a-f]{12}$/u.test(selected.accountDisplayId)))
  ) {
    return undefined;
  }
  return {
    providerId: selected.providerId,
    accountDisplayId: selected.accountDisplayId,
  };
}

function sanitizeDecisionTraceExclusions(value) {
  const items = ownDataArray(value, MAX_DECISION_TRACE_EXCLUSIONS);
  if (items === null) return null;
  const exclusions = [];
  for (const item of items) {
    const exclusion = ownDataRecord(item, ["accountDisplayId", "reason"]);
    if (
      exclusion === null ||
      typeof exclusion.accountDisplayId !== "string" ||
      !/^acct_[0-9a-f]{12}$/u.test(exclusion.accountDisplayId) ||
      !DECISION_TRACE_EXCLUSION_REASONS.has(exclusion.reason)
    ) {
      return null;
    }
    exclusions.push({
      accountDisplayId: exclusion.accountDisplayId,
      reason: exclusion.reason,
    });
  }
  return exclusions;
}

function sanitizeDecisionTraceFallback(value) {
  if (value === null) return null;
  const fallback = ownDataRecord(value, [
    "attempted",
    "attemptCount",
    "finalAction",
  ]);
  if (
    fallback === null ||
    typeof fallback.attempted !== "boolean" ||
    !Number.isInteger(fallback.attemptCount) ||
    fallback.attemptCount < 0 ||
    fallback.attemptCount > 2 ||
    !DECISION_TRACE_FALLBACK_ACTIONS.has(fallback.finalAction) ||
    (!fallback.attempted &&
      (fallback.attemptCount > 1 || fallback.finalAction !== "none")) ||
    (fallback.attempted &&
      (fallback.attemptCount < 1 || fallback.finalAction === "none")) ||
    ((fallback.finalAction === "retry" ||
      fallback.finalAction === "degrade_to_cheap") &&
      fallback.attemptCount !== 2)
  ) {
    return undefined;
  }
  return {
    attempted: fallback.attempted,
    attemptCount: fallback.attemptCount,
    finalAction: fallback.finalAction,
  };
}

function sanitizeDecisionTraceFactsWindow(value) {
  if (value === null) return null;
  const factsWindow = ownDataRecord(value, ["durationSeconds"]);
  if (
    factsWindow === null ||
    !Number.isInteger(factsWindow.durationSeconds) ||
    factsWindow.durationSeconds < 1 ||
    factsWindow.durationSeconds > MAX_DECISION_TRACE_FACTS_WINDOW_SECONDS
  ) {
    return undefined;
  }
  return { durationSeconds: factsWindow.durationSeconds };
}

function isCanonicalDecisionTraceTimestamp(value) {
  if (typeof value !== "string") return false;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z$/u.exec(
    value,
  );
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    0,
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth[month] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}

function sanitizeAccountPoolAccount(value) {
  if (
    !isJsonRecord(value) ||
    !hasExactOwnKeys(value, [
      "providerId",
      "alias",
      "displayId",
      "status",
      "quota",
      "cooldown",
      "pools",
      "priority",
      "weight",
    ])
  ) {
    return null;
  }
  const providerId = boundedShouldSendText(value.providerId, 128);
  const alias = value.alias === null
    ? null
    : boundedShouldSendText(value.alias, 128);
  const displayId = boundedShouldSendText(value.displayId, 128);
  const quota = sanitizeAccountQuota(value.quota);
  const cooldown = sanitizeAccountCooldown(value.cooldown);
  const pools = sanitizeAccountPoolMemberships(value.pools);
  if (
    providerId === null ||
    !isPublicPoolId(providerId) ||
    (value.alias !== null && alias === null) ||
    displayId === null ||
    !/^acct_[0-9a-f]{12}$/u.test(displayId) ||
    !ACCOUNT_POOL_STATUSES.has(value.status) ||
    quota === null ||
    cooldown === null ||
    pools === null ||
    !isBoundedPublicInteger(value.priority) ||
    !isBoundedPublicInteger(value.weight)
  ) {
    return null;
  }
  return {
    providerId,
    alias,
    displayId,
    status: value.status,
    quota,
    cooldown,
    pools,
    priority: value.priority,
    weight: value.weight,
  };
}

function sanitizeAccountPoolPublicPool(value) {
  if (
    !isJsonRecord(value) ||
    !hasExactOwnKeys(value, [
      "poolId",
      "revision",
      "strategy",
      "members",
      "crossProviderFallback",
      "crossModelFallback",
      "crossRegionFallback",
    ]) ||
    !isPublicPoolId(value.poolId) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    !ACCOUNT_POOL_STRATEGIES.has(value.strategy) ||
    typeof value.crossProviderFallback !== "boolean" ||
    typeof value.crossModelFallback !== "boolean" ||
    typeof value.crossRegionFallback !== "boolean"
  ) {
    return null;
  }
  const members = sanitizeHostActionPoolMembers(value.members);
  if (members === null) return null;
  return {
    poolId: value.poolId,
    revision: value.revision,
    strategy: value.strategy,
    members,
    crossProviderFallback: value.crossProviderFallback,
    crossModelFallback: value.crossModelFallback,
    crossRegionFallback: value.crossRegionFallback,
  };
}

function sanitizeAccountQuota(value) {
  if (
    !isJsonRecord(value) ||
    !hasExactOwnKeys(value, ["state", "remaining", "limit", "resetAt"]) ||
    !ACCOUNT_QUOTA_STATES.has(value.state)
  ) {
    return null;
  }
  const remaining = nullableNonNegativeNumber(value.remaining);
  const limit = nullableNonNegativeNumber(value.limit);
  if (
    remaining === undefined ||
    limit === undefined ||
    !isValidShouldSendTimestamp(value.resetAt)
  ) {
    return null;
  }
  return { state: value.state, remaining, limit, resetAt: value.resetAt };
}

function sanitizeAccountCooldown(value) {
  if (
    !isJsonRecord(value) ||
    !hasExactOwnKeys(value, ["state", "until"]) ||
    !ACCOUNT_COOLDOWN_STATES.has(value.state) ||
    !isValidShouldSendTimestamp(value.until)
  ) {
    return null;
  }
  return { state: value.state, until: value.until };
}

function sanitizeAccountPoolMemberships(value) {
  if (!Array.isArray(value) || value.length > 64) return null;
  const result = [];
  const seen = new Set();
  for (const item of value) {
    if (
      !isJsonRecord(item) ||
      !hasExactOwnKeys(item, ["poolId", "priority", "weight"])
    ) {
      return null;
    }
    const poolId = boundedShouldSendText(item.poolId, 128);
    if (
      poolId === null ||
      !/^[A-Za-z0-9._-]+$/u.test(poolId) ||
      seen.has(poolId) ||
      !isBoundedPublicInteger(item.priority) ||
      !isBoundedPublicInteger(item.weight)
    ) {
      return null;
    }
    seen.add(poolId);
    result.push({ poolId, priority: item.priority, weight: item.weight });
  }
  return result;
}

function boundedProviderConfigText(value, limit) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > limit ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    return null;
  }
  return value;
}

function sanitizeProviderConfigApplyRequest(body) {
  const request = ownDataRecord(body, [
    "apiVersion",
    "action",
    "presetId",
    "apiKey",
    "baseUrl",
    "model",
  ]);
  if (
    request === null ||
    request.apiVersion !== HOST_ACTION_API_VERSION ||
    request.action !== PROVIDER_CONFIG_APPLY_ACTION
  ) {
    return null;
  }
  const presetId = boundedProviderConfigText(request.presetId, 256);
  const apiKey = boundedProviderConfigText(request.apiKey, PROVIDER_CONFIG_FIELD_LIMIT);
  const baseUrl =
    request.baseUrl === null
      ? null
      : boundedProviderConfigText(request.baseUrl, PROVIDER_CONFIG_FIELD_LIMIT);
  const model =
    request.model === null
      ? null
      : boundedProviderConfigText(request.model, 1024);
  if (presetId === null || apiKey === null) return null;
  if (request.baseUrl !== null && baseUrl === null) return null;
  if (request.model !== null && model === null) return null;
  return {
    apiVersion: HOST_ACTION_API_VERSION,
    action: PROVIDER_CONFIG_APPLY_ACTION,
    command: {
      version: PROVIDER_CONFIG_MUTATE_VERSION,
      action: "provider_config_apply",
      presetId,
      apiKey,
      baseUrl,
      model,
    },
  };
}

function sanitizeHostActionRequest(body) {
  if (body === null || typeof body !== "object") return null;
  if (body.action === PROVIDER_CONFIG_APPLY_ACTION) {
    return sanitizeProviderConfigApplyRequest(body);
  }
  const request = ownDataRecord(body, [
    "apiVersion",
    "action",
    "expectedRevision",
    "pool",
  ]);
  if (
    request === null ||
    request.apiVersion !== HOST_ACTION_API_VERSION
  ) {
    return null;
  }
  if (request.action === ACCOUNT_POOL_CREATE_ACTION) {
    if (request.expectedRevision !== null) return null;
    const pool = sanitizeHostActionCreatePool(request.pool);
    if (pool === null) return null;
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action: ACCOUNT_POOL_CREATE_ACTION,
      command: {
        version: ACCOUNT_POOL_MUTATE_VERSION,
        action: "create_pool",
        pool,
        expectedRevision: null,
      },
    };
  }
  if (request.action === ACCOUNT_POOL_EDIT_ACTION) {
    if (!isHostActionRevision(request.expectedRevision)) return null;
    const pool = sanitizeHostActionCreatePool(request.pool);
    if (pool === null) return null;
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action: ACCOUNT_POOL_EDIT_ACTION,
      command: {
        version: ACCOUNT_POOL_MUTATE_VERSION,
        action: "edit_pool",
        pool,
        expectedRevision: request.expectedRevision,
      },
    };
  }
  if (request.action === ACCOUNT_POOL_REMOVE_ACTION) {
    if (!isHostActionRevision(request.expectedRevision)) return null;
    const pool = sanitizeHostActionRemovePool(request.pool);
    if (pool === null) return null;
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action: ACCOUNT_POOL_REMOVE_ACTION,
      command: {
        version: ACCOUNT_POOL_MUTATE_VERSION,
        action: "remove_pool",
        pool,
        expectedRevision: request.expectedRevision,
      },
    };
  }
  return null;
}

function sanitizeGatewayAccountOperationRequest(body) {
  if (body === null || typeof body !== "object") return null;
  const actionDescriptor = Object.getOwnPropertyDescriptor(body, "action");
  if (
    actionDescriptor === undefined ||
    !Object.prototype.hasOwnProperty.call(actionDescriptor, "value")
  ) {
    return null;
  }
  const action = actionDescriptor.value;
  if (action === GATEWAY_ACCOUNT_OPEN_CREATE) {
    const request = ownDataRecord(body, ["apiVersion", "action", "preset"]);
    if (
      request === null ||
      request.apiVersion !== GATEWAY_ACCOUNT_HOST_API_VERSION ||
      request.action !== GATEWAY_ACCOUNT_OPEN_CREATE ||
      !GATEWAY_ACCOUNT_PRESETS.has(request.preset)
    ) {
      return null;
    }
    return request;
  }
  if (
    action === GATEWAY_ACCOUNT_OPEN_EDIT ||
    action === GATEWAY_ACCOUNT_OPEN_REPLACE ||
    action === GATEWAY_ACCOUNT_OPEN_REMOVE
  ) {
    const request = ownDataRecord(body, ["apiVersion", "action", "displayId"]);
    if (
      request === null ||
      request.apiVersion !== GATEWAY_ACCOUNT_HOST_API_VERSION ||
      request.action !== action ||
      typeof request.displayId !== "string" ||
      !/^acct_[0-9a-f]{12}$/u.test(request.displayId)
    ) {
      return null;
    }
    return request;
  }
  return null;
}

function sanitizeHostActionRequestBody(body) {
  const value = parseStrictJsonObject(body, MAX_HOST_ACTION_REQUEST_BYTES);
  if (value === null) return null;
  return sanitizeHostActionRequest(value);
}

function hostActionCapabilities({
  hostActionExecutor,
  providerConfigExecutor,
} = {}) {
  const actions = [];
  if (validHostActionExecutor(hostActionExecutor)) {
    actions.push(
      ACCOUNT_POOL_CREATE_ACTION,
      ACCOUNT_POOL_EDIT_ACTION,
      ACCOUNT_POOL_REMOVE_ACTION,
    );
  }
  if (validHostActionExecutor(providerConfigExecutor)) {
    actions.push(PROVIDER_CONFIG_APPLY_ACTION);
  }
  return { apiVersion: HOST_ACTION_API_VERSION, actions };
}

function gatewayAccountOperationCapabilities({
  gatewayAccountEditorExecutor,
  platform,
} = {}) {
  return {
    apiVersion: GATEWAY_ACCOUNT_HOST_API_VERSION,
    actions: validGatewayAccountEditorExecutor(
      gatewayAccountEditorExecutor,
      platform,
    )
      ? [...GATEWAY_ACCOUNT_ACTIONS]
      : [],
  };
}

function executeHostActionCommand(
  actionRequest,
  {
    hostActionExecutor,
    providerConfigExecutor,
    spawnProcess = childProcess.spawn,
    deadlineMs = DEFAULT_DEADLINE_MS,
  } = {},
) {
  const executor =
    actionRequest !== null &&
    typeof actionRequest === "object" &&
    actionRequest.action === PROVIDER_CONFIG_APPLY_ACTION
      ? providerConfigExecutor
      : hostActionExecutor;
  if (
    actionRequest === null ||
    typeof actionRequest !== "object" ||
    !validHostActionExecutor(executor) ||
    typeof spawnProcess !== "function"
  ) {
    return Promise.resolve(null);
  }
  const input = Buffer.from(JSON.stringify(actionRequest.command), "utf8");
  if (input.length === 0 || input.length > MAX_HOST_ACTION_REQUEST_BYTES) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    let child;
    try {
      child = spawnProcess(executor.command, executor.args, {
        shell: false,
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch {
      resolve(null);
      return;
    }
    if (
      child === null ||
      typeof child !== "object" ||
      typeof child.once !== "function" ||
      typeof child.kill !== "function" ||
      child.stdin === null ||
      typeof child.stdin !== "object" ||
      typeof child.stdin.write !== "function" ||
      typeof child.stdin.end !== "function" ||
      child.stdout === null ||
      typeof child.stdout !== "object" ||
      typeof child.stdout.on !== "function" ||
      child.stderr === null ||
      typeof child.stderr !== "object" ||
      typeof child.stderr.on !== "function"
    ) {
      terminateChild(child);
      resolve(null);
      return;
    }
    let settled = false;
    let stdoutLength = 0;
    let stderrLength = 0;
    const stdout = [];
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      resolve(value);
    };
    const fail = () => {
      terminateChild(child);
      finish(null);
    };
    const deadline = setTimeout(fail, validDeadline(deadlineMs));
    child.stdout.on("data", (chunk) => {
      if (settled || !Buffer.isBuffer(chunk)) {
        fail();
        return;
      }
      stdoutLength += chunk.length;
      if (stdoutLength > MAX_HOST_ACTION_RESPONSE_BYTES) {
        fail();
        return;
      }
      stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      if (settled || !Buffer.isBuffer(chunk)) {
        fail();
        return;
      }
      stderrLength += chunk.length;
      if (stderrLength > MAX_HOST_ACTION_RESPONSE_BYTES) fail();
    });
    child.once("error", fail);
    child.once("close", (code, signal) => {
      if (settled) return;
      if ((code !== 0 && code !== 1) || signal !== null) {
        finish(null);
        return;
      }
      const result = sanitizeHostActionCommandResult(
        Buffer.concat(stdout, stdoutLength),
        actionRequest.action,
      );
      if (result === null || result.ok !== (code === 0)) {
        finish(null);
        return;
      }
      finish(result);
    });
    try {
      child.stdin.write(input);
      child.stdin.end();
    } catch {
      fail();
    }
  });
}

function sanitizeProviderConfigCommandResult(value, action) {
  if (
    !hasExactOwnKeys(value, [
      "version",
      "ok",
      "code",
      "agent",
      "presetId",
      "name",
      "category",
      "status",
    ]) ||
    value.version !== PROVIDER_CONFIG_MUTATE_VERSION ||
    typeof value.ok !== "boolean" ||
    typeof value.code !== "string" ||
    !/^[a-z_]{1,64}$/u.test(value.code)
  ) {
    return null;
  }
  if (!value.ok) {
    return {
      apiVersion: HOST_ACTION_API_VERSION,
      action,
      ok: false,
      code: value.code,
    };
  }
  if (
    typeof value.agent !== "string" ||
    !PROVIDER_CONFIG_AGENTS.has(value.agent) ||
    typeof value.presetId !== "string" ||
    value.presetId.length === 0 ||
    value.presetId.length > 256 ||
    typeof value.name !== "string" ||
    value.name.length === 0 ||
    value.name.length > 256 ||
    typeof value.category !== "string" ||
    !PROVIDER_CONFIG_CATEGORIES.has(value.category) ||
    typeof value.status !== "string" ||
    !PROVIDER_CONFIG_STATUSES.has(value.status)
  ) {
    return null;
  }
  return {
    apiVersion: HOST_ACTION_API_VERSION,
    action,
    ok: true,
    code: value.code,
    agent: value.agent,
    presetId: value.presetId,
    name: value.name,
    category: value.category,
    status: value.status,
  };
}

function sanitizeHostActionCommandResult(body, action) {
  const value = parseStrictJsonObject(body, MAX_HOST_ACTION_RESPONSE_BYTES);
  if (value === null) return null;
  if (action === PROVIDER_CONFIG_APPLY_ACTION) {
    return sanitizeProviderConfigCommandResult(value, action);
  }
  const keys = Object.keys(value);
  if (
    !(
      hasExactOwnKeys(value, ["version", "ok", "code"]) ||
      hasExactOwnKeys(value, ["version", "ok", "code", "pool"])
    ) ||
    value.version !== ACCOUNT_POOL_MUTATE_VERSION ||
    typeof value.ok !== "boolean" ||
    typeof value.code !== "string" ||
    !/^[a-z_]{1,64}$/u.test(value.code)
  ) {
    return null;
  }
  const result = {
    apiVersion: HOST_ACTION_API_VERSION,
    action,
    ok: value.ok,
    code: value.code,
  };
  if (keys.includes("pool")) {
    const pool = sanitizeHostActionResultPool(value.pool);
    if (pool === null) return null;
    result.pool = pool;
  }
  return result;
}

function sanitizeHostActionResultPool(value) {
  if (
    !isJsonRecord(value) ||
    !hasExactOwnKeys(value, ["poolId", "revision"]) ||
    !isPublicPoolId(value.poolId) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  ) {
    return null;
  }
  return { poolId: value.poolId, revision: value.revision };
}

function validHostActionExecutor(value) {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.command !== "string" ||
    value.command.length === 0 ||
    value.command.length > 4096 ||
    /[\u0000-\u001f\u007f]/u.test(value.command) ||
    !(path.isAbsolute(value.command) || path.win32.isAbsolute(value.command)) ||
    !Array.isArray(value.args) ||
    value.args.length !== 1 ||
    !HOST_ACTION_SUBCOMMANDS.has(value.args[0])
  ) {
    return false;
  }
  return true;
}

function validGatewayAccountEditorExecutor(value, platform) {
  return isGatewayAccountEditorExecutor(value, platform);
}

function isHostActionRevision(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function sanitizeHostActionRemovePool(value) {
  const pool = ownDataRecord(value, ["poolId"]);
  if (pool === null || !isPublicPoolId(pool.poolId)) return null;
  return {
    poolId: pool.poolId,
  };
}

function sanitizeHostActionCreatePool(value) {
  const pool = ownDataRecord(value, [
    "poolId",
    "strategy",
    "members",
    "crossProviderFallback",
    "crossModelFallback",
    "crossRegionFallback",
  ]);
  if (
    pool === null ||
    !isPublicPoolId(pool.poolId) ||
    !ACCOUNT_POOL_STRATEGIES.has(pool.strategy) ||
    typeof pool.crossProviderFallback !== "boolean" ||
    typeof pool.crossModelFallback !== "boolean" ||
    typeof pool.crossRegionFallback !== "boolean"
  ) {
    return null;
  }
  const members = sanitizeHostActionPoolMembers(pool.members);
  if (members === null) return null;
  return {
    poolId: pool.poolId,
    strategy: pool.strategy,
    members,
    crossProviderFallback: pool.crossProviderFallback,
    crossModelFallback: pool.crossModelFallback,
    crossRegionFallback: pool.crossRegionFallback,
  };
}

function sanitizeHostActionPoolMembers(value) {
  const items = ownDataArray(value, MAX_ACCOUNT_POOL_MEMBERS);
  if (items === null || items.length === 0) return null;
  const members = [];
  const displayIds = new Set();
  for (const item of items) {
    const member = ownDataRecord(item, ["displayId", "priority", "weight"]);
    if (
      member === null ||
      !/^acct_[0-9a-f]{12}$/u.test(member.displayId) ||
      displayIds.has(member.displayId) ||
      !Number.isInteger(member.priority) ||
      member.priority < 0 ||
      member.priority > 10_000 ||
      !Number.isInteger(member.weight) ||
      member.weight < 1 ||
      member.weight > 100
    ) {
      return null;
    }
    displayIds.add(member.displayId);
    members.push({
      displayId: member.displayId,
      priority: member.priority,
      weight: member.weight,
    });
  }
  return members;
}

function isPublicPoolId(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_ACCOUNT_POOL_ID_LENGTH &&
    /^[A-Za-z0-9._-]+$/u.test(value)
  );
}

function ownDataRecord(value, expectedKeys) {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    types.isProxy(value)
  ) {
    return null;
  }
  let prototype;
  let descriptors;
  try {
    prototype = Object.getPrototypeOf(value);
    descriptors = Object.getOwnPropertyDescriptors(value);
  } catch {
    return null;
  }
  if (prototype !== Object.prototype) return null;
  const expected = new Set(expectedKeys);
  const keys = Reflect.ownKeys(descriptors);
  if (
    keys.length !== expectedKeys.length ||
    keys.some((key) => typeof key !== "string" || !expected.has(key))
  ) {
    return null;
  }
  const result = {};
  for (const key of expectedKeys) {
    const descriptor = descriptors[key];
    if (
      descriptor === undefined ||
      !Object.prototype.hasOwnProperty.call(descriptor, "value") ||
      descriptor.enumerable !== true
    ) {
      return null;
    }
    result[key] = descriptor.value;
  }
  return result;
}

function ownDataArray(value, maximumLength) {
  if (
    !Array.isArray(value) ||
    types.isProxy(value) ||
    !Number.isInteger(value.length) ||
    value.length < 0 ||
    value.length > maximumLength
  ) {
    return null;
  }
  let prototype;
  let descriptors;
  try {
    prototype = Object.getPrototypeOf(value);
    descriptors = Object.getOwnPropertyDescriptors(value);
  } catch {
    return null;
  }
  if (prototype !== Array.prototype) return null;
  const keys = Reflect.ownKeys(descriptors);
  if (keys.length !== value.length + 1) return null;
  const lengthDescriptor = descriptors.length;
  if (
    lengthDescriptor === undefined ||
    !Object.prototype.hasOwnProperty.call(lengthDescriptor, "value") ||
    lengthDescriptor.value !== value.length
  ) {
    return null;
  }
  const items = [];
  for (let index = 0; index < value.length; index += 1) {
    const key = String(index);
    const descriptor = descriptors[key];
    if (
      descriptor === undefined ||
      !Object.prototype.hasOwnProperty.call(descriptor, "value") ||
      descriptor.enumerable !== true
    ) {
      return null;
    }
    items.push(descriptor.value);
  }
  return items;
}

function isBoundedPublicInteger(value) {
  return Number.isInteger(value) && value >= 0 && value <= 1_000_000;
}

function parseStrictJsonObject(
  body,
  maximumBytes,
  { topLevelLexemes } = {},
) {
  if (
    !Buffer.isBuffer(body) ||
    body.length === 0 ||
    body.length > maximumBytes
  ) {
    return null;
  }
  try {
    const text = new TextDecoder("utf-8", {
      fatal: true,
      ignoreBOM: true,
    }).decode(body);
    if (!hasUniqueJsonObjectKeys(text, topLevelLexemes)) return null;
    const value = JSON.parse(text);
    return isJsonRecord(value) ? value : null;
  } catch {
    return null;
  }
}

function hasUniqueJsonObjectKeys(text, topLevelLexemes) {
  let index = 0;
  const skipWhitespace = () => {
    while (
      index < text.length &&
      (text[index] === " " ||
        text[index] === "\t" ||
        text[index] === "\r" ||
        text[index] === "\n")
    ) {
      index += 1;
    }
  };
  const scanString = () => {
    if (text[index] !== '"') return null;
    const start = index;
    index += 1;
    while (index < text.length) {
      const character = text[index];
      if (character === "\\") {
        index += 2;
        continue;
      }
      index += 1;
      if (character === '"') {
        try {
          return { value: JSON.parse(text.slice(start, index)) };
        } catch {
          return null;
        }
      }
    }
    return null;
  };
  const scanValue = (depth) => {
    if (depth > 16) return false;
    skipWhitespace();
    if (text[index] === '"') return scanString() !== null;
    if (text[index] === "{") return scanObject(depth + 1);
    if (text[index] === "[") return scanArray(depth + 1);
    const start = index;
    while (
      index < text.length &&
      text[index] !== "," &&
      text[index] !== "]" &&
      text[index] !== "}" &&
      text[index] !== " " &&
      text[index] !== "\t" &&
      text[index] !== "\r" &&
      text[index] !== "\n"
    ) {
      index += 1;
    }
    return index > start;
  };
  const scanArray = (depth) => {
    index += 1;
    skipWhitespace();
    if (text[index] === "]") {
      index += 1;
      return true;
    }
    while (index < text.length) {
      if (!scanValue(depth)) return false;
      skipWhitespace();
      if (text[index] === "]") {
        index += 1;
        return true;
      }
      if (text[index] !== ",") return false;
      index += 1;
    }
    return false;
  };
  const scanObject = (depth) => {
    index += 1;
    const keys = new Set();
    skipWhitespace();
    if (text[index] === "}") {
      index += 1;
      return true;
    }
    while (index < text.length) {
      skipWhitespace();
      const key = scanString();
      if (key === null || keys.has(key.value)) return false;
      keys.add(key.value);
      skipWhitespace();
      if (text[index] !== ":") return false;
      index += 1;
      skipWhitespace();
      const valueStart = index;
      if (!scanValue(depth)) return false;
      if (depth === 0 && topLevelLexemes instanceof Map) {
        topLevelLexemes.set(key.value, text.slice(valueStart, index));
      }
      skipWhitespace();
      if (text[index] === "}") {
        index += 1;
        return true;
      }
      if (text[index] !== ",") return false;
      index += 1;
    }
    return false;
  };
  skipWhitespace();
  if (text[index] !== "{" || !scanObject(0)) return false;
  skipWhitespace();
  return index === text.length;
}

function hasExactOwnKeys(value, expected) {
  const keys = Object.keys(value);
  return (
    keys.length === expected.length &&
    hasRequiredOwnKeys(value, expected)
  );
}

function hasRequiredOwnKeys(value, expected) {
  return expected.every((key) =>
    Object.prototype.hasOwnProperty.call(value, key),
  );
}

function isJsonRecord(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function boundedShouldSendText(value, maximumLength) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    /\p{C}/u.test(value)
  ) {
    return null;
  }
  let length = 0;
  for (const _character of value) {
    length += 1;
    if (length > maximumLength) return null;
  }
  return value;
}

function nullableNonNegativeNumber(value) {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

function isValidShouldSendTimestamp(value) {
  if (value === null) return true;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_SHOULD_SEND_TIMESTAMP_LENGTH ||
    value !== value.trim()
  ) {
    return false;
  }
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(?:Z|([+-])(\d{2}):(\d{2}))$/u.exec(
      value,
    );
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = match[6] === undefined ? 0 : Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    0,
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth[month] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    offsetHour <= 23 &&
    offsetMinute <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}

function parsePrivateObserverJson(body) {
  if (!Buffer.isBuffer(body) || body.length > MAX_LOCAL_RESPONSE_BYTES) {
    return null;
  }
  try {
    const payload = JSON.parse(body.toString("utf8"));
    return payload !== null &&
      typeof payload === "object" &&
      !Array.isArray(payload)
      ? payload
      : null;
  } catch {
    return null;
  }
}

function requestFailure(kind) {
  const error = new Error("private service unavailable");
  error.kind = kind;
  return error;
}

function failureCode(error) {
  if (error?.kind === "timeout") return "gateway_timeout";
  if (error?.kind === "invalid") return "capability_invalid";
  return "gateway_unavailable";
}

function requestErrorKind(error, timedOut) {
  if (timedOut) return "timeout";
  return typeof error?.code === "string" && error.code.startsWith("HPE_")
    ? "invalid"
    : "unavailable";
}

function isRendererApiTarget(target) {
  return (
    typeof target === "string" &&
    (target.startsWith("/v1") ||
      target.startsWith("/gateway") ||
      target.startsWith("/host/"))
  );
}

function createStaticFileHandler({ webRoot, mimeTypes = {} } = {}) {
  return function handleStaticFile(request, response) {
    const headOnly = request?.method === "HEAD";
    if (request?.method !== "GET" && !headOnly) {
      sendStaticFailure(response, 405, "method not allowed", false, {
        Allow: "GET, HEAD",
      });
      return;
    }
    const resolved = resolveStaticFile(webRoot, request?.url);
    if (resolved.statusCode !== 200) {
      sendStaticFailure(
        response,
        resolved.statusCode,
        resolved.statusCode === 400 ? "bad request" : "not found",
        headOnly,
      );
      return;
    }

    const extension = path.extname(resolved.filePath).toLowerCase();
    const configuredType = Object.prototype.hasOwnProperty.call(
      mimeTypes,
      extension,
    )
      ? mimeTypes[extension]
      : null;
    const contentType =
      typeof configuredType === "string" &&
      configuredType.length <= 128 &&
      !/[\r\n\u0000\u007f]/u.test(configuredType)
        ? configuredType
        : "application/octet-stream";
    response.writeHead(200, {
      "Cache-Control": "no-cache",
      "Content-Length": String(resolved.size),
      "Content-Type": contentType,
      "X-Content-Type-Options": "nosniff",
    });
    if (headOnly) {
      response.end();
      return;
    }
    const stream = fs.createReadStream(resolved.filePath);
    stream.once("error", () => {
      if (!response.headersSent) {
        sendStaticFailure(response, 404, "not found", false);
      } else {
        response.destroy();
      }
    });
    stream.pipe(response);
  };
}

function fetchLegacyDashboardJson(
  target,
  {
    requestFactory = http.request,
    maxBytes = MAX_LEGACY_JSON_BYTES,
    maxChunks = MAX_LEGACY_JSON_CHUNKS,
    deadlineMs = LEGACY_JSON_DEADLINE_MS,
  } = {},
) {
  if (
    typeof target !== "string" ||
    target.length === 0 ||
    target.length > MAX_TARGET_LENGTH ||
    /[\r\n\u0000\u007f]/u.test(target) ||
    typeof requestFactory !== "function"
  ) {
    return Promise.resolve(null);
  }
  const byteLimit =
    Number.isInteger(maxBytes) && maxBytes >= 1 && maxBytes <= 4 * 1024 * 1024
      ? maxBytes
      : MAX_LEGACY_JSON_BYTES;
  const chunkLimit =
    Number.isInteger(maxChunks) && maxChunks >= 1 && maxChunks <= 4096
      ? maxChunks
      : MAX_LEGACY_JSON_CHUNKS;
  const deadlineLimit =
    Number.isInteger(deadlineMs) && deadlineMs >= 50 && deadlineMs <= 30_000
      ? deadlineMs
      : LEGACY_JSON_DEADLINE_MS;

  return new Promise((resolve) => {
    let settled = false;
    let request;
    let response;
    let deadline;
    const finish = (value, destroy = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      if (destroy) {
        try {
          response?.destroy();
        } catch {
          // Collapse boundary cleanup failures into the same null result.
        }
        try {
          request?.destroy();
        } catch {
          // Collapse boundary cleanup failures into the same null result.
        }
      }
      resolve(value);
    };
    try {
      request = requestFactory(
        target,
        { agent: false, method: "GET" },
        (incoming) => {
          response = incoming;
          if (incoming?.statusCode !== 200) {
            finish(null, true);
            return;
          }
          const declaredLength = incoming.headers?.["content-length"];
          if (
            typeof declaredLength === "string" &&
            /^[0-9]{1,10}$/u.test(declaredLength) &&
            Number(declaredLength) > byteLimit
          ) {
            finish(null, true);
            return;
          }
          const chunks = [];
          let total = 0;
          let chunkCount = 0;
          incoming.on("data", (chunk) => {
            if (settled) return;
            chunkCount += 1;
            if (chunkCount > chunkLimit) {
              finish(null, true);
              return;
            }
            if (!Buffer.isBuffer(chunk) && typeof chunk !== "string") {
              finish(null, true);
              return;
            }
            const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
            total += bytes.length;
            if (total > byteLimit) {
              finish(null, true);
              return;
            }
            chunks.push(bytes);
          });
          incoming.on("end", () => {
            if (settled) return;
            try {
              finish(JSON.parse(Buffer.concat(chunks, total).toString("utf8")));
            } catch {
              finish(null);
            }
          });
          incoming.on("aborted", () => finish(null, true));
          incoming.on("error", () => finish(null, true));
        },
      );
      if (
        request === null ||
        typeof request !== "object" ||
        typeof request.on !== "function" ||
        typeof request.end !== "function"
      ) {
        finish(null, true);
        return;
      }
      request.on("error", () => finish(null, true));
      deadline = setTimeout(() => finish(null, true), deadlineLimit);
      request.end();
    } catch {
      finish(null, true);
    }
  });
}

function resolveStaticFile(webRoot, target) {
  if (
    typeof webRoot !== "string" ||
    webRoot.length === 0 ||
    webRoot.length > 4096 ||
    !path.isAbsolute(webRoot) ||
    /[\u0000-\u001f\u007f]/u.test(webRoot) ||
    typeof target !== "string" ||
    target.length === 0 ||
    target.length > MAX_STATIC_TARGET_LENGTH ||
    !target.startsWith("/") ||
    target.startsWith("//") ||
    /[\u0000-\u001f\u007f\\#]/u.test(target)
  ) {
    return { statusCode: 400 };
  }
  const question = target.indexOf("?");
  const rawPath = question === -1 ? target : target.slice(0, question);
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(rawPath);
  } catch {
    return { statusCode: 400 };
  }
  if (
    !decodedPath.startsWith("/") ||
    decodedPath.startsWith("//") ||
    decodedPath.includes("//") ||
    /[\u0000-\u001f\u007f\\#]/u.test(decodedPath)
  ) {
    return { statusCode: 400 };
  }
  if (decodedPath.split("/").includes("..")) {
    return { statusCode: 404 };
  }

  try {
    const root = fs.realpathSync.native(webRoot);
    if (!fs.statSync(root).isDirectory()) return { statusCode: 404 };
    const relativePath = decodedPath === "/" ? "index.html" : decodedPath.slice(1);
    const candidate = path.resolve(root, relativePath);
    if (!isContainedPath(root, candidate)) return { statusCode: 404 };

    let filePath = candidate;
    try {
      const candidateRealPath = fs.realpathSync.native(candidate);
      if (!isContainedPath(root, candidateRealPath)) return { statusCode: 404 };
      const candidateStats = fs.statSync(candidateRealPath);
      if (candidateStats.isFile()) {
        return {
          statusCode: 200,
          filePath: candidateRealPath,
          size: candidateStats.size,
        };
      }
      if (!candidateStats.isDirectory()) return { statusCode: 404 };
    } catch (error) {
      if (error?.code !== "ENOENT" && error?.code !== "ENOTDIR") {
        return { statusCode: 404 };
      }
    }

    filePath = fs.realpathSync.native(path.join(root, "index.html"));
    if (!isContainedPath(root, filePath)) return { statusCode: 404 };
    const indexStats = fs.statSync(filePath);
    return indexStats.isFile()
      ? { statusCode: 200, filePath, size: indexStats.size }
      : { statusCode: 404 };
  } catch {
    return { statusCode: 404 };
  }
}

function isContainedPath(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative === "" ||
    (relative !== ".." &&
      !relative.startsWith(`..${path.sep}`) &&
      !path.isAbsolute(relative))
  );
}

function sendStaticFailure(
  response,
  statusCode,
  message,
  headOnly,
  extraHeaders = {},
) {
  const body = Buffer.from(message, "utf8");
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Length": String(body.length),
    "Content-Type": "text/plain; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  response.end(headOnly ? undefined : body);
}

function readRendererShouldSendBody(request, declaredLength, deadlineMs) {
  return readRendererBoundedBody(
    request,
    declaredLength,
    MAX_SHOULD_SEND_REQUEST_BYTES,
    deadlineMs,
  );
}

function readRendererHostActionBody(request, declaredLength, deadlineMs) {
  return readRendererBoundedBody(
    request,
    declaredLength,
    MAX_HOST_ACTION_REQUEST_BYTES,
    deadlineMs,
  );
}

function readRendererBoundedBody(request, declaredLength, maximumBytes, deadlineMs) {
  return new Promise((resolve, reject) => {
    if (
      !Number.isInteger(declaredLength) ||
      declaredLength < 1 ||
      declaredLength > maximumBytes ||
      request === null ||
      typeof request !== "object" ||
      typeof request.on !== "function"
    ) {
      reject(requestFailure("invalid"));
      return;
    }
    let settled = false;
    let total = 0;
    let deadline;
    const chunks = [];
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      callback(value);
    };
    request.on("data", (chunk) => {
      if (settled) return;
      if (!Buffer.isBuffer(chunk)) {
        finish(reject, requestFailure("invalid"));
        return;
      }
      total += chunk.length;
      if (
        total > declaredLength ||
        total > maximumBytes
      ) {
        finish(reject, requestFailure("invalid"));
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (total !== declaredLength) {
        finish(reject, requestFailure("invalid"));
        return;
      }
      finish(resolve, Buffer.concat(chunks, total));
    });
    request.on("aborted", () => finish(reject, requestFailure("invalid")));
    request.on("error", () => finish(reject, requestFailure("invalid")));
    deadline = setTimeout(
      () => finish(reject, requestFailure("timeout")),
      validDeadline(deadlineMs),
    );
  });
}

function rendererHeader(headers, expectedName) {
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() === expectedName) return value;
  }
  return undefined;
}

function rendererJsonResponse(payload, statusCode = 200) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  return {
    statusCode,
    headers: {
      "Cache-Control": "no-store",
      "Content-Length": String(body.length),
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    },
    body,
  };
}

function createRendererApiHandler(options) {
  const gatewayAccountOperationHost = createGatewayAccountOperationHost({
    executor: options?.gatewayAccountEditorExecutor,
    spawnProcess: options?.spawnProcess ?? childProcess.spawn,
    randomBytes: options?.gatewayAccountOperationRandomBytes,
    readyDeadlineMs: options?.gatewayAccountOperationReadyDeadlineMs,
    lifetimeMs: options?.gatewayAccountOperationLifetimeMs,
    terminalTtlMs: options?.gatewayAccountOperationTerminalTtlMs,
    maxRecords: options?.gatewayAccountOperationMaxRecords,
    now: options?.gatewayAccountOperationNow,
    platform: options?.platform,
  });
  const handlerOptions = { ...options, gatewayAccountOperationHost };
  return async function handleRendererApi(request, response) {
    let headers;
    let classified;
    try {
      headers = distinctRequestHeaders(request);
      classified =
        headers === null
          ? null
          : classifyRendererRequest({
              method: request.method,
              target: request.url,
              headers,
            });
    } catch {
      classified = null;
    }
    if (classified === null) {
      if (
        request.method !== "GET" &&
        request.method !== "HEAD"
      ) {
        sendSafeProblemAndClose(request, response, 404, "not_found");
        return;
      }
      sendSafeProblem(response, 404, "not_found", request.method === "HEAD");
      return;
    }
    try {
      if (
        classified.method === "POST" &&
        classified.target === "/host/v2/gateway-account-operations"
      ) {
        const declaredLength = Number(rendererHeader(headers, "content-length"));
        let submitted;
        try {
          submitted = await readRendererHostActionBody(
            request,
            declaredLength,
            options?.deadlineMs,
          );
        } catch {
          sendSafeProblemAndClose(
            request,
            response,
            502,
            "service_unavailable",
          );
          return;
        }
        const value = parseStrictJsonObject(
          submitted,
          MAX_HOST_ACTION_REQUEST_BYTES,
        );
        const gatewayAccountIntent = sanitizeGatewayAccountOperationRequest(value);
        if (gatewayAccountIntent === null) {
          sendSafeProblem(response, 400, "invalid_intent", false);
          return;
        }
        classified = { ...classified, gatewayAccountIntent };
      }
      if (
        classified.method === "POST" &&
        classified.target === "/gateway/v1/should-send"
      ) {
        const declaredLength = Number(rendererHeader(headers, "content-length"));
        let submitted;
        try {
          submitted = await readRendererShouldSendBody(
            request,
            declaredLength,
            options?.deadlineMs,
          );
        } catch {
          sendSafeProblemAndClose(
            request,
            response,
            502,
            "service_unavailable",
          );
          return;
        }
        const body = canonicalShouldSendRequestBody(submitted);
        if (body === null) {
          sendSafeProblem(response, 400, "invalid_request", false);
          return;
        }
        classified = { ...classified, body };
      }
      if (
        classified.method === "POST" &&
        classified.target === "/host/v1/actions"
      ) {
        const declaredLength = Number(rendererHeader(headers, "content-length"));
        let submitted;
        try {
          submitted = await readRendererHostActionBody(
            request,
            declaredLength,
            options?.deadlineMs,
          );
        } catch {
          sendSafeProblemAndClose(
            request,
            response,
            502,
            "service_unavailable",
          );
          return;
        }
        const actionRequest = sanitizeHostActionRequestBody(submitted);
        if (actionRequest === null) {
          sendSafeProblem(response, 400, "invalid_request", false);
          return;
        }
        classified = { ...classified, actionRequest };
      }
      const result = await fetchRendererResponse(classified, handlerOptions);
      response.writeHead(result.statusCode, result.headers);
      if (request.method === "HEAD") {
        response.end();
      } else {
        response.end(result.body);
      }
    } catch {
      sendSafeProblem(
        response,
        502,
        "service_unavailable",
        request.method === "HEAD",
      );
    }
  };
}

function distinctRequestHeaders(request) {
  const source = request?.headersDistinct;
  if (source && typeof source === "object") {
    const result = {};
    for (const [name, values] of Object.entries(source)) {
      if (!Array.isArray(values) || values.length !== 1) return null;
      result[name] = values[0];
    }
    return result;
  }
  return request?.headers && typeof request.headers === "object"
    ? request.headers
    : null;
}

function sendSafeProblemAndClose(request, response, statusCode, code) {
  const socket = request?.socket;
  const closeAfterFlush = () => {
    try {
      if (typeof socket?.destroySoon === "function") {
        socket.destroySoon();
      } else if (typeof socket?.end === "function") {
        socket.end();
      }
    } catch {
      try {
        socket?.destroy();
      } catch {
        // The stable response has already crossed the public boundary.
      }
    }
  };
  response.once("finish", closeAfterFlush);
  sendSafeProblem(response, statusCode, code, false, {
    Connection: "close",
  });
  const forceClose = setTimeout(() => {
    try {
      socket?.destroy();
    } catch {
      // The connection remains bounded by the server's own close handling.
    }
  }, 250);
  if (typeof forceClose.unref === "function") forceClose.unref();
  if (typeof socket?.once === "function") {
    socket.once("close", () => clearTimeout(forceClose));
  }
}

function sendSafeProblem(
  response,
  statusCode,
  code,
  headOnly,
  extraHeaders = {},
) {
  const body = Buffer.from(JSON.stringify({ error: { code } }), "utf8");
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Length": String(body.length),
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  response.end(headOnly ? undefined : body);
}

module.exports = {
  RUNTIME_CAPABILITY_MEDIA_TYPE,
  classifyRendererRequest,
  createRendererApiHandler,
  createStaticFileHandler,
  createWindowsAclVerifier,
  discoverPrivateRuntime,
  fetchPrivateObserverJson,
  fetchRendererResponse,
  fetchLegacyDashboardJson,
  isRendererApiTarget,
  observerDaemonArguments,
  probePrivateObserver,
  readPrivateToken,
  sanitizeAccountPoolsPayload,
  sanitizeDecisionTracesPayload,
  sanitizeDecisionTracesValue,
  sanitizeHostActionRequest,
  sanitizeGatewayAccountOperationRequest,
  startOrProbeLegacyDashboard,
  startOrProbePrivateObserver,
  validatePrivateUnixSocket,
};
