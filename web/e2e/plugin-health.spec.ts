import { expect, test, type Page, type Route } from "@playwright/test";

const timestamp = "2026-08-10T03:04:05.123456Z";
const PRIVATE_CANARY = "PRIVATE_PLUGIN_TOKEN_PATH_CANARY_31d2";

function pluginConnectionsEnvelope() {
  return {
    apiVersion: "plugin.openusage/v1",
    object: "plugin.connections",
    observedAt: timestamp,
    connections: [
      {
        pluginId: "loom",
        configuration: "configured",
        connection: "connected",
        capabilityState: "negotiated",
        capabilities: [
          "decision.lookup",
          "health.query",
          "outcome.record",
          "quotas.query",
          "route.advice",
          "usage.query",
        ],
        lastSeenAt: timestamp,
        lastSyncOutcome: "succeeded",
        lastSyncAt: timestamp,
      },
      {
        pluginId: "codex",
        configuration: "configured",
        connection: "stale",
        capabilityState: "negotiated",
        capabilities: [
          "decision.lookup",
          "health.query",
          "outcome.record",
          "quotas.query",
          "route.advice",
          "usage.query",
        ],
        lastSeenAt: timestamp,
        lastSyncOutcome: "failed",
        lastSyncAt: timestamp,
      },
      {
        pluginId: "claude_code",
        configuration: "not_configured",
        connection: "never_seen",
        capabilityState: "not_negotiated",
        capabilities: [],
        lastSeenAt: null,
        lastSyncOutcome: "never",
        lastSyncAt: null,
      },
    ],
  };
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("usagehub.lang", "en"));
});

test("Data Health separates plugin configuration, connection, capabilities, and recent sync", async ({ page }) => {
  await installDataHealthRoutes(page, async (route) => json(route, pluginConnectionsEnvelope()));
  await openPluginHealth(page);

  const panel = pluginPanel(page);
  await expect(panel).toContainText(
    "Optional integrations do not affect Observer or Gateway. Configuration and connection do not prove Provider health or a successful sync.",
  );
  const loom = pluginRow(page, "Loom");
  await expect(loom).toContainText("Configured");
  await expect(loom).toContainText("Connected");
  await expect(loom).toContainText("Negotiated");
  await expect(loom).toContainText("Succeeded");
  await expect(loom).toContainText("Decision lookup");

  const codex = pluginRow(page, "Codex");
  await expect(codex).toContainText("Stale");
  await expect(codex).toContainText("Failed");

  const claude = pluginRow(page, "Claude Code");
  await expect(claude).toContainText("Not configured");
  await expect(claude).toContainText("Never seen");
  await expect(claude).toContainText("Never synced");
  await expect(panel).not.toContainText(/installation|Provider healthy/iu);
  await expect(panel).not.toContainText(/decision\.lookup|health\.query|usage\.query/u);
});

test("standalone host failure is unavailable rather than not configured", async ({ page }) => {
  await installDataHealthRoutes(page, async (route) =>
    json(route, { error: { code: "service_unavailable" } }, 404),
  );
  await openPluginHealth(page);
  await expect(pluginPanel(page)).toContainText(
    "Integration status is unavailable. The desktop host may be offline or unsupported.",
  );
  await expect(pluginPanel(page).locator(".plugin-connection-item")).toHaveCount(0);
  await expect(pluginPanel(page)).not.toContainText("Not configured");
});

test("invalid private projection fails closed without rendering its canary", async ({ page }) => {
  await installDataHealthRoutes(page, async (route) =>
    json(route, { ...pluginConnectionsEnvelope(), tokenPath: PRIVATE_CANARY }),
  );
  await openPluginHealth(page);
  await expect(pluginPanel(page)).toContainText("Integration status is unavailable.");
  await expect(page.locator("body")).not.toContainText(PRIVATE_CANARY);
});

test("keyboard refresh retains last-known plugin facts without moving focus", async ({ page }) => {
  let fail = false;
  await installDataHealthRoutes(page, async (route) =>
    fail
      ? json(route, { error: { code: "service_unavailable" } }, 502)
      : json(route, pluginConnectionsEnvelope()),
  );
  await openPluginHealth(page);
  fail = true;
  const refresh = pluginPanel(page).getByRole("button", { name: "Refresh integrations" });
  await refresh.focus();
  await page.keyboard.press("Enter");
  await expect(pluginPanel(page)).toContainText(
    "Integration status could not be refreshed. Showing the last known status.",
  );
  await expect(pluginRow(page, "Loom")).toContainText("Connected");
  await expect(pluginPanel(page).getByRole("button", { name: "Retry integrations" })).toBeFocused();
});

test("controlled plugin abort retains last-good, settles its live region, and does not steal focus", async ({ page }) => {
  let refreshing = false;
  let refreshRequestCount = 0;
  let signalRefreshStarted!: () => void;
  const refreshStarted = new Promise<void>((resolve) => {
    signalRefreshStarted = resolve;
  });
  let releaseRefresh!: () => void;
  const refreshTerminal = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  await installDataHealthRoutes(page, async (route) => {
    if (!refreshing) return json(route, pluginConnectionsEnvelope());
    refreshRequestCount += 1;
    signalRefreshStarted();
    await refreshTerminal;
    return route.abort("aborted");
  });
  await openPluginHealth(page);

  const panel = pluginPanel(page);
  const refresh = panel.getByRole("button", { name: "Refresh integrations" });
  refreshing = true;
  await refresh.focus();
  await page.keyboard.press("Enter");
  await refreshStarted;
  await expect(panel).toHaveAttribute("aria-busy", "true");
  await expect(refresh).toBeDisabled();
  await refresh.evaluate((button: HTMLButtonElement) => button.click());
  expect(refreshRequestCount).toBe(1);
  await expect(pluginRow(page, "Loom")).toContainText("Connected");
  const announcer = page.locator(".service-status-announcement[role=status]");
  await expect(announcer).toHaveCount(1);
  await expect(announcer).toHaveAttribute("aria-live", "polite");
  await expect(announcer).toHaveAttribute("aria-atomic", "true");
  await expect(announcer).toContainText(
    "Refreshing integration status…",
  );

  const allSources = page.getByRole("button", { name: "All sources" });
  await allSources.focus();
  await expect(allSources).toBeFocused();
  releaseRefresh();

  await expect(panel).toHaveAttribute("aria-busy", "false");
  await expect(panel).toContainText(
    "Integration status could not be refreshed. Showing the last known status.",
  );
  await expect(pluginRow(page, "Loom")).toContainText("Connected");
  await expect(panel.getByRole("button", { name: "Retry integrations" })).toBeEnabled();
  await expect(announcer).toContainText(
    "Integration status could not be refreshed. Showing the last known status.",
  );
  await expect(allSources).toBeFocused();
  expect(refreshRequestCount).toBe(1);
});

test("Observer Platform HTTP failure cannot expose its private response body", async ({ page }) => {
  await installDataHealthRoutes(
    page,
    async (route) => json(route, pluginConnectionsEnvelope()),
    async (route) => json(route, { raw_error: PRIVATE_CANARY }, 502),
  );
  await openPluginHealth(page);
  await expect(page.locator(".service-status-strip")).toContainText(
    "Showing the last known status",
  );
  await expect(page.getByRole("region", { name: "Gateway", exact: true })).toContainText(
    "Ready",
  );
  await expect(page.locator("body")).not.toContainText(PRIVATE_CANARY);
});

test("plugin host failure does not change Observer or Gateway status", async ({ page }) => {
  await installDataHealthRoutes(page, async (route) =>
    json(route, { error: { code: "service_unavailable" } }, 502),
  );
  await openPluginHealth(page);
  await expect(page.getByRole("region", { name: "Observer", exact: true })).toContainText("Ready");
  await expect(page.getByRole("region", { name: "Gateway", exact: true })).toContainText("Ready");
  await expect(pluginPanel(page)).toContainText("Integration status is unavailable.");
  await expect(page.getByRole("region", { name: "Observer sources" })).toContainText(
    "No source status yet. Sources appear after collection starts.",
  );
});

test("plugin health renders the same factual boundaries in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("usagehub.lang", "zh"));
  await installDataHealthRoutes(page, async (route) => json(route, pluginConnectionsEnvelope()));
  await page.goto("/data-health");
  const panel = page.getByRole("region", { name: "可选集成" });
  await expect(panel).toContainText("已配置或已连接不代表 Provider 健康或同步成功");
  await expect(panel).toContainText("从未同步");
  await expect(panel).not.toContainText("同步历史未知");
});

for (const viewport of [
  { width: 320, height: 720 },
  { width: 768, height: 900 },
  { width: 1440, height: 1000 },
]) {
  test(`plugin health fits ${viewport.width}px without horizontal overflow`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installDataHealthRoutes(page, async (route) => json(route, pluginConnectionsEnvelope()));
    await openPluginHealth(page);
    await expect(pluginPanel(page).locator(".plugin-connection-list")).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.innerWidth);
  });
}

async function openPluginHealth(page: Page) {
  await page.goto("/data-health");
  await expect(page.getByRole("heading", { name: "Optional integrations" })).toBeVisible();
}

function pluginPanel(page: Page) {
  return page.getByRole("region", { name: "Optional integrations" });
}

function pluginRow(page: Page, name: string) {
  return pluginPanel(page).getByRole("listitem").filter({
    has: page.getByRole("heading", { name, exact: true }),
  });
}

async function installDataHealthRoutes(
  page: Page,
  pluginResponse: (route: Route) => Promise<unknown>,
  observerPlatformResponse?: (route: Route) => Promise<unknown>,
) {
  await page.route("**/*", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/v1/providers") return json(route, { providers: [] });
    if (pathname === "/v1/sources/status") return json(route, { sources: [] });
    if (pathname === "/v1/quick-connect") return json(route, { providers: [] });
    if (pathname === "/gateway/v1/health") return json(route, runtimeCapability());
    if (pathname === "/v1/capabilities") {
      if (observerPlatformResponse) {
        await observerPlatformResponse(route);
        return;
      }
      return json(route, {
        observerPlatform: {
          operatingSystem: "linux",
          support: "supported",
          supportedSourceCount: 2,
          totalSourceCount: 50,
          reasonCode: "supported_sources_available",
        },
      });
    }
    if (pathname === "/host/v1/plugin-connections") {
      await pluginResponse(route);
      return;
    }
    return route.continue();
  });
}

function runtimeCapability() {
  const features = Object.fromEntries([
    "listener",
    "should_send",
    "responses",
    "cache",
    "fallback",
    "pii_redaction",
    "streaming",
  ].map((id) => [id, {
    support: "supported",
    enabled: true,
    configured: true,
    operational: "ready",
  }]));
  return {
    apiVersion: "runtime-capability.openusage/v1",
    object: "runtime.capability",
    observer: {
      operational: "ready",
      generatedAt: "2026-08-10T03:04:05Z",
      lastGoodAt: "2026-08-10T03:04:05Z",
      dataRevision: 42,
      schemaVersion: "openusage/v1",
    },
    gateway: {
      mode: "gateway",
      operational: "ready",
      features,
      configuredProviderCount: 1,
      healthyProviderCount: 1,
      actions: ["retry"],
      lastError: null,
    },
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
