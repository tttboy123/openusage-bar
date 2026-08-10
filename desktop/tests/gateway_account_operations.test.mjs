import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import http from "node:http";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);

const API_VERSION = "gateway-account-host.openusage/v1";
const actions = [
  "gatewayAccount.openCreate",
  "gatewayAccount.openEdit",
  "gatewayAccount.openReplace",
  "gatewayAccount.openRemove",
];

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

function request(port, target, { method = "GET", json } = {}) {
  const body = json === undefined ? null : Buffer.from(JSON.stringify(json));
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: "127.0.0.1",
      port,
      method,
      path: target,
      headers: body === null
        ? {}
        : { "Content-Type": "application/json", "Content-Length": body.length },
    }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({
        statusCode: res.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    req.once("error", reject);
    req.end(body ?? undefined);
  });
}

function fakeEditorChild({ onInput } = {}) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdin = {
    chunks: [],
    write(chunk) {
      this.chunks.push(Buffer.from(chunk));
    },
    end() {
      onInput?.(Buffer.concat(this.chunks));
    },
  };
  child.kill = () => {
    child.killed = true;
  };
  return child;
}

test("Gateway account operation sanitizer accepts only the exact public Gateway account intents", () => {
  const { sanitizeGatewayAccountOperationRequest } = require("../gateway_proxy.js");

  for (const preset of ["openai", "anthropic", "deepseek", "openrouter"]) {
    assert.deepEqual(
      sanitizeGatewayAccountOperationRequest({
        apiVersion: API_VERSION,
        action: actions[0],
        preset,
      }),
      { apiVersion: API_VERSION, action: actions[0], preset },
    );
  }
  for (const action of actions.slice(1)) {
    assert.deepEqual(
      sanitizeGatewayAccountOperationRequest({
        apiVersion: API_VERSION,
        action,
        displayId: "acct_0123456789ab",
      }),
      { apiVersion: API_VERSION, action, displayId: "acct_0123456789ab" },
    );
  }
});

test("Gateway account operation sanitizer rejects private fields, unsupported presets, prototypes, and accessors", () => {
  const { sanitizeGatewayAccountOperationRequest } = require("../gateway_proxy.js");
  const create = {
    apiVersion: API_VERSION,
    action: actions[0],
    preset: "openai",
  };
  const edit = {
    apiVersion: API_VERSION,
    action: actions[1],
    displayId: "acct_0123456789ab",
  };
  const inherited = Object.create({ apiVersion: API_VERSION });
  Object.assign(inherited, { action: actions[0], preset: "openai" });
  const accessor = { apiVersion: API_VERSION, action: actions[0] };
  Object.defineProperty(accessor, "preset", {
    enumerable: true,
    get() {
      throw new Error("must not invoke hostile accessor");
    },
  });

  for (const request of [
    ...["minimax", "moonshot", "step_plan", "codex", "custom"].map((preset) => ({
      ...create,
      preset,
    })),
    { ...create, token: "sk-private" },
    { ...create, credential: "private" },
    { ...create, header: "Authorization" },
    { ...create, endpoint: "https://private.invalid" },
    { ...create, path: "/private/account" },
    { ...create, env: { API_KEY: "private" } },
    { ...create, command: "private-tool" },
    { ...create, accountId: "private-account-id" },
    { ...edit, providerId: "openai" },
    { ...edit, preset: "openai" },
    { ...edit, displayId: "openai-primary" },
    inherited,
    accessor,
  ]) {
    assert.equal(sanitizeGatewayAccountOperationRequest(request), null);
  }
});

test("Gateway account capability endpoint exposes the exact v2 actions only for the packaged editor", async (context) => {
  const { createRendererApiHandler, isRendererApiTarget } = require("../gateway_proxy.js");
  assert.equal(isRendererApiTarget("/host/v2/gateway-account-capabilities"), true);
  assert.equal(isRendererApiTarget("/host/v2/gateway-account-operations"), true);

  const accessorExecutor = {};
  Object.defineProperty(accessorExecutor, "command", {
    enumerable: true,
    get() {
      throw new Error("must not read an inherited editor command");
    },
  });
  accessorExecutor.args = ["gateway-account-editor"];
  for (const [gatewayAccountEditorExecutor, enabled, platform = "linux"] of [
    [null, false],
    [{ command: "relative/openusage-settings", args: ["gateway-account-editor"] }, false],
    [{ command: "/opt/openusage-settings\nprivate", args: ["gateway-account-editor"] }, false],
    [{ command: "/opt/openusage-settings", args: ["provider-editor"] }, false],
    [{ command: "/opt/openusage-settings", args: ["gateway-account-editor"], env: {} }, false],
    [accessorExecutor, false],
    [{ command: "C:\\Program Files\\UsageHub\\openusage-settings.exe", args: ["gateway-account-editor"] }, false, "linux"],
    [{ command: "C:\\Program Files\\UsageHub\\openusage-settings.exe", args: ["gateway-account-editor"] }, true, "win32"],
    [{ command: "/opt/UsageHub/resources/settings/openusage-settings", args: ["gateway-account-editor"] }, true],
  ]) {
    const server = http.createServer(createRendererApiHandler({
      runtime: null,
      platform,
      gatewayAccountEditorExecutor,
    }));
    const port = await listen(server);
    context.after(() => close(server));
    const response = await request(port, "/host/v2/gateway-account-capabilities");
    assert.equal(response.statusCode, 200);
    assert.deepEqual(response.body, {
      apiVersion: API_VERSION,
      actions: enabled ? actions : [],
    });
  }
});

test("openCreate launches one shell-free editor with public intent only and returns an opaque operation", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  const spawnCalls = [];
  let child;
  const spawnProcess = (command, args, options) => {
    child = fakeEditorChild({
      onInput(input) {
        assert.deepEqual(JSON.parse(input.toString("utf8")), {
          apiVersion: API_VERSION,
          action: actions[0],
          preset: "anthropic",
        });
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    spawnCalls.push({ command, args, options });
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
  }));
  const port = await listen(server);
  context.after(async () => {
    child?.emit("close", 1, null);
    await close(server);
  });

  const response = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: { apiVersion: API_VERSION, action: actions[0], preset: "anthropic" },
  });
  assert.equal(response.statusCode, 202);
  assert.equal(response.body.apiVersion, API_VERSION);
  assert.equal(response.body.state, "opened");
  assert.match(response.body.operationId, /^op_[0-9a-f]{32}$/u);
  assert.doesNotMatch(response.body.operationId, /anthropic|openai|acct|provider/iu);
  assert.deepEqual(spawnCalls, [{
    command: "/opt/UsageHub/resources/settings/openusage-settings",
    args: ["gateway-account-editor"],
    options: {
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    },
  }]);
});

test("only one editor can be active and busy is a stable allowlisted failure", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  let child;
  let spawnCount = 0;
  const spawnProcess = () => {
    spawnCount += 1;
    child = fakeEditorChild({
      onInput() {
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
  }));
  const port = await listen(server);
  context.after(() => close(server));
  const intent = { apiVersion: API_VERSION, action: actions[0], preset: "deepseek" };

  assert.equal(
    (await request(port, "/host/v2/gateway-account-operations", { method: "POST", json: intent })).statusCode,
    202,
  );
  const busy = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: intent,
  });
  assert.equal(busy.statusCode, 409);
  assert.deepEqual(busy.body, { error: { code: "helper_busy" } });
  assert.equal(spawnCount, 1);

  child.stdout.emit(
    "data",
    Buffer.from('{"version":1,"event":"terminal","state":"cancelled","code":"cancelled"}\n'),
  );
  child.emit("close", 0, null);
});

test("missing exact ready fails at the three-second boundary without leaking helper output", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  let child;
  const spawnProcess = () => {
    child = fakeEditorChild();
    process.nextTick(() => {
      child.stderr.emit("data", Buffer.from("PRIVATE CANARY exception path"));
    });
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
    gatewayAccountOperationReadyDeadlineMs: 50,
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const response = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: { apiVersion: API_VERSION, action: actions[0], preset: "openai" },
  });
  assert.equal(response.statusCode, 502);
  assert.deepEqual(response.body, { error: { code: "service_unavailable" } });
  assert.equal(child.killed, true);
  assert.doesNotMatch(JSON.stringify(response.body), /CANARY|exception|path/iu);
});

test("invalid public intent returns only the frozen safe error", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  let spawnCount = 0;
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess() {
      spawnCount += 1;
      throw new Error("must not spawn");
    },
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const response = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: {
      apiVersion: API_VERSION,
      action: actions[0],
      preset: "codex",
      token: "PRIVATE CANARY",
    },
  });
  assert.equal(response.statusCode, 400);
  assert.deepEqual(response.body, { error: { code: "invalid_intent" } });
  assert.equal(spawnCount, 0);
  assert.doesNotMatch(JSON.stringify(response.body), /CANARY|token|codex/iu);
});

test("poll exposes only pending and an allowlisted terminal state and code", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  let child;
  const spawnProcess = () => {
    child = fakeEditorChild({
      onInput() {
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const opened = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: { apiVersion: API_VERSION, action: actions[2], displayId: "acct_0123456789ab" },
  });
  const pollTarget = `/host/v2/gateway-account-operations/${opened.body.operationId}`;
  assert.deepEqual((await request(port, pollTarget)).body, {
    apiVersion: API_VERSION,
    operationId: opened.body.operationId,
    state: "pending",
  });

  child.stderr.emit("data", Buffer.from("PRIVATE CANARY helper detail"));
  child.stdout.emit(
    "data",
    Buffer.from('{"version":1,"event":"terminal","state":"succeeded","code":"ok"}\n'),
  );
  child.emit("close", 0, null);
  const terminal = await request(port, pollTarget);
  assert.equal(terminal.statusCode, 200);
  assert.deepEqual(terminal.body, {
    apiVersion: API_VERSION,
    operationId: opened.body.operationId,
    state: "succeeded",
    code: "ok",
  });
  assert.doesNotMatch(JSON.stringify(terminal.body), /CANARY|stderr|exception|detail/iu);
});

test("terminal state-code and process-exit pairs fail closed", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  const children = [];
  const spawnProcess = () => {
    const child = fakeEditorChild({
      onInput() {
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    children.push(child);
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const cases = [
    { state: "cancelled", code: "cancelled", exitCode: 0, expectedState: "cancelled", expectedCode: "cancelled" },
    { state: "failed", code: "not_found", exitCode: 1, expectedState: "failed", expectedCode: "not_found" },
    { state: "succeeded", code: "not_found", exitCode: 0, expectedState: "failed", expectedCode: "service_unavailable" },
  ];
  for (const [index, item] of cases.entries()) {
    const opened = await request(port, "/host/v2/gateway-account-operations", {
      method: "POST",
      json: { apiVersion: API_VERSION, action: actions[1], displayId: "acct_0123456789ab" },
    });
    const child = children[index];
    child.stdout.emit("data", Buffer.from(`${JSON.stringify({
      version: 1,
      event: "terminal",
      state: item.state,
      code: item.code,
    })}\n`));
    child.emit("close", item.exitCode, null);
    assert.deepEqual(
      (await request(port, `/host/v2/gateway-account-operations/${opened.body.operationId}`)).body,
      {
        apiVersion: API_VERSION,
        operationId: opened.body.operationId,
        state: item.expectedState,
        code: item.expectedCode,
      },
    );
  }
});

test("a Gateway account editor is killed at the bounded total lifetime", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  let child;
  const spawnProcess = () => {
    child = fakeEditorChild({
      onInput() {
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
    gatewayAccountOperationLifetimeMs: 50,
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const opened = await request(port, "/host/v2/gateway-account-operations", {
    method: "POST",
    json: { apiVersion: API_VERSION, action: actions[3], displayId: "acct_0123456789ab" },
  });
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(child.killed, true);
  assert.deepEqual(
    (await request(port, `/host/v2/gateway-account-operations/${opened.body.operationId}`)).body,
    {
      apiVersion: API_VERSION,
      operationId: opened.body.operationId,
      state: "timed_out",
      code: "timed_out",
    },
  );
});

test("the operation registry evicts terminal records by count and TTL", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  const children = [];
  const spawnProcess = () => {
    const child = fakeEditorChild({
      onInput() {
        process.nextTick(() => {
          child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
        });
      },
    });
    children.push(child);
    return child;
  };
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    spawnProcess,
    gatewayAccountOperationMaxRecords: 2,
    gatewayAccountOperationTerminalTtlMs: 50,
  }));
  const port = await listen(server);
  context.after(() => close(server));

  const operationIds = [];
  for (let index = 0; index < 3; index += 1) {
    const opened = await request(port, "/host/v2/gateway-account-operations", {
      method: "POST",
      json: { apiVersion: API_VERSION, action: actions[0], preset: "openrouter" },
    });
    operationIds.push(opened.body.operationId);
    const child = children[index];
    child.stdout.emit(
      "data",
      Buffer.from('{"version":1,"event":"terminal","state":"succeeded","code":"ok"}\n'),
    );
    child.emit("close", 0, null);
  }

  assert.equal(
    (await request(port, `/host/v2/gateway-account-operations/${operationIds[0]}`)).statusCode,
    404,
  );
  assert.equal(
    (await request(port, `/host/v2/gateway-account-operations/${operationIds[2]}`)).body.state,
    "succeeded",
  );
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(
    (await request(port, `/host/v2/gateway-account-operations/${operationIds[2]}`)).statusCode,
    404,
  );
});

test("operation IDs retry collisions without encoding public intent", async (context) => {
  const { createRendererApiHandler } = require("../gateway_proxy.js");
  const children = [];
  let randomCall = 0;
  const server = http.createServer(createRendererApiHandler({
    runtime: null,
    gatewayAccountEditorExecutor: {
      command: "/opt/UsageHub/resources/settings/openusage-settings",
      args: ["gateway-account-editor"],
    },
    gatewayAccountOperationRandomBytes(length) {
      assert.equal(length, 16);
      randomCall += 1;
      return Buffer.alloc(16, randomCall <= 2 ? 0x11 : 0x22);
    },
    spawnProcess() {
      const child = fakeEditorChild({
        onInput() {
          process.nextTick(() => {
            child.stdout.emit("data", Buffer.from('{"version":1,"event":"ready"}\n'));
          });
        },
      });
      children.push(child);
      return child;
    },
  }));
  const port = await listen(server);
  context.after(() => close(server));
  const ids = [];

  for (const preset of ["openai", "anthropic"]) {
    const opened = await request(port, "/host/v2/gateway-account-operations", {
      method: "POST",
      json: { apiVersion: API_VERSION, action: actions[0], preset },
    });
    ids.push(opened.body.operationId);
    const child = children.at(-1);
    child.stdout.emit(
      "data",
      Buffer.from('{"version":1,"event":"terminal","state":"succeeded","code":"ok"}\n'),
    );
    child.emit("close", 0, null);
  }

  assert.deepEqual(ids, [
    `op_${"11".repeat(16)}`,
    `op_${"22".repeat(16)}`,
  ]);
  assert.equal(randomCall, 3);
});
