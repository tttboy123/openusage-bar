import { expect, test, type Page, type Route } from "@playwright/test";

const API_VERSION = "gateway-account-host.openusage/v1";
const DISPLAY_ID = "acct_0123456789ab";
const OPERATION_ID = "op_0123456789abcdef0123456789abcdef";
const OPERATIONS_PATH = "/host/v2/gateway-account-operations";
const PRIVATE_INTENT_KEYS = new Set([
  "credential",
  "token",
  "header",
  "accountId",
  "providerId",
  "endpoint",
  "path",
  "env",
  "command",
]);

type HostMode = "trusted" | "standalone";

const accountPoolsSnapshot = {
  accounts: [
    {
      alias: "Primary Gateway account",
      displayId: DISPLAY_ID,
      providerId: "openai",
      status: "ready",
      quota: { state: "available", remaining: 80, limit: 100, resetAt: null },
      cooldown: { state: "inactive", until: null },
      pools: [],
      priority: 10,
      weight: 1,
    },
  ],
  pools: [],
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("usagehub.lang", "en");
  });
});

test("standalone Web is explicitly read-only and renders no Gateway host actions", async ({ page }) => {
  await installGatewayRoutes(page, "standalone", []);
  await openProviders(page);

  await expect(
    page.getByText(/^Gateway account management is unavailable here\./),
  ).toBeVisible();
  for (const name of gatewayActionNames) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
});

test("trusted create dialog owns focus, traps Tab, supports Escape, and restores focus", async ({ page }) => {
  await installGatewayRoutes(page, "trusted", []);
  await openProviders(page);

  const trigger = page.getByRole("button", { name: "Add Gateway account" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Add Gateway account" });
  const providerSelect = dialog.getByRole("combobox", { name: "Gateway Provider" });
  await expect(providerSelect).toBeFocused();

  await dialog.getByRole("button", { name: "Continue in secure window" }).focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

for (const viewport of [
  { width: 320, height: 720 },
  { width: 768, height: 900 },
  { width: 1440, height: 1000 },
]) {
  test(`trusted account controls fit ${viewport.width}px without horizontal overflow`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installGatewayRoutes(page, "trusted", []);
    await openProviders(page);

    await expect(page.getByRole("button", { name: "Add Gateway account" })).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.innerWidth);
    await expect(gatewayAccountCard(page)).toBeVisible();
  });
}

const intentCases = [
  {
    name: "create",
    action: "gatewayAccount.openCreate",
    expected: { apiVersion: API_VERSION, action: "gatewayAccount.openCreate", preset: "openai" },
  },
  {
    name: "edit",
    action: "gatewayAccount.openEdit",
    buttonName: "Edit account",
    expected: { apiVersion: API_VERSION, action: "gatewayAccount.openEdit", displayId: DISPLAY_ID },
  },
  {
    name: "replace",
    action: "gatewayAccount.openReplace",
    buttonName: "Replace credential",
    expected: { apiVersion: API_VERSION, action: "gatewayAccount.openReplace", displayId: DISPLAY_ID },
  },
  {
    name: "remove",
    action: "gatewayAccount.openRemove",
    buttonName: "Remove account",
    expected: { apiVersion: API_VERSION, action: "gatewayAccount.openRemove", displayId: DISPLAY_ID },
  },
] as const;

for (const intentCase of intentCases) {
  test(`${intentCase.name} sends one exact secret-free public intent`, async ({ page }) => {
    const intents: unknown[] = [];
    await installGatewayRoutes(page, "trusted", intents);
    await openProviders(page);

    if (intentCase.action === "gatewayAccount.openCreate") {
      await page.getByRole("button", { name: "Add Gateway account" }).click();
      await page.getByRole("button", { name: "Continue in secure window" }).click();
    } else {
      await gatewayAccountCard(page)
        .getByRole("button", { name: intentCase.buttonName })
        .click();
    }

    await expect.poll(() => intents.length).toBe(1);
    expect(intents[0]).toEqual(intentCase.expected);
    expect(privateKeysIn(intents[0])).toEqual([]);
  });
}

test("launching, opened, or pending work disables Add, edit, replace, and remove", async ({ page }) => {
  const intents: unknown[] = [];
  let releaseLaunch!: () => void;
  const launchGate = new Promise<void>((resolve) => {
    releaseLaunch = resolve;
  });
  await installGatewayRoutes(page, "trusted", intents, launchGate);
  await openProviders(page);

  await gatewayAccountCard(page).getByRole("button", { name: "Edit account" }).click();
  await expect.poll(() => intents.length).toBe(1);
  for (const name of gatewayActionNames) {
    await expect(page.getByRole("button", { name })).toBeDisabled();
  }
  releaseLaunch();
  await expect(page.getByText(/^Secure window opened\./)).toBeVisible();
  for (const name of gatewayActionNames) {
    await expect(page.getByRole("button", { name })).toBeDisabled();
  }
});

const gatewayActionNames = [
  "Add Gateway account",
  "Edit account",
  "Replace credential",
  "Remove account",
];

async function openProviders(page: Page) {
  await page.goto("/providers");
  await expect(gatewayAccountCard(page)).toBeVisible();
}

function gatewayAccountCard(page: Page) {
  return page.getByRole("listitem").filter({ hasText: DISPLAY_ID });
}

async function installGatewayRoutes(
  page: Page,
  mode: HostMode,
  intents: unknown[],
  launchGate?: Promise<void>,
) {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());

    if (pathname === "/v1/providers") return json(route, { providers: [] });
    if (pathname === "/v1/sources/status") return json(route, { sources: [] });
    if (pathname === "/v1/quick-connect") return json(route, { providers: [] });
    if (pathname === "/gateway/v1/account-pools") return json(route, accountPoolsSnapshot);
    if (pathname === "/host/v1/capabilities") {
      return json(route, { error: { code: "service_unavailable" } }, 404);
    }
    if (pathname === "/host/v2/gateway-account-capabilities") {
      if (mode === "standalone") {
        return json(route, { error: { code: "service_unavailable" } }, 404);
      }
      return json(route, {
        apiVersion: API_VERSION,
        actions: [
          "gatewayAccount.openCreate",
          "gatewayAccount.openEdit",
          "gatewayAccount.openReplace",
          "gatewayAccount.openRemove",
        ],
      });
    }
    if (pathname === OPERATIONS_PATH && request.method() === "POST") {
      intents.push(request.postDataJSON());
      await launchGate;
      return json(route, { apiVersion: API_VERSION, operationId: OPERATION_ID, state: "opened" });
    }
    if (pathname === `${OPERATIONS_PATH}/${OPERATION_ID}`) {
      return json(route, { apiVersion: API_VERSION, operationId: OPERATION_ID, state: "pending" });
    }
    return route.continue();
  });
}

function privateKeysIn(value: unknown): string[] {
  if (value === null || typeof value !== "object") return [];
  const found: string[] = [];
  for (const [key, child] of Object.entries(value)) {
    if (PRIVATE_INTENT_KEYS.has(key)) found.push(key);
    found.push(...privateKeysIn(child));
  }
  return found;
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
