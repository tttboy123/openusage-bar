import { expect, test, type Page, type Route } from "@playwright/test";

const API_VERSION = "gateway-decision-trace.openusage/v1";
const PRIVATE_CANARY = "PRIVATE_PROMPT_TOKEN_CANARY_713e";

const decisionTraceEnvelope = {
  apiVersion: API_VERSION,
  traces: [
    {
      traceId: "trace_00000000000000000000000000000003",
      occurredAt: "2026-08-10T12:00:03.000000Z",
      kind: "route_advice",
      execution: "advice_only",
      outcome: "defer",
      reason: "quota_low",
      pool: null,
      selected: null,
      exclusions: [],
      fallback: null,
      factsWindow: { durationSeconds: 300 },
    },
    {
      traceId: "trace_00000000000000000000000000000002",
      occurredAt: "2026-08-10T12:00:02.000000Z",
      kind: "gateway_execution",
      execution: "executed",
      outcome: "succeeded",
      reason: null,
      pool: null,
      selected: { providerId: "openai", accountDisplayId: null },
      exclusions: [],
      fallback: { attempted: false, attemptCount: 0, finalAction: "none" },
      factsWindow: null,
    },
    {
      traceId: "trace_00000000000000000000000000000001",
      occurredAt: "2026-08-10T12:00:01.000000Z",
      kind: "pool_selection",
      execution: "executed",
      outcome: "selected",
      reason: null,
      pool: { poolId: "primary-us", revision: 7, strategy: "quota-aware" },
      selected: {
        providerId: "anthropic",
        accountDisplayId: "acct_1234567890ab",
      },
      exclusions: [
        { accountDisplayId: "acct_abcdef123456", reason: "cooldown" },
      ],
      fallback: null,
      factsWindow: null,
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("usagehub.lang", "en");
  });
});

test("Automation distinguishes advice from execution and renders only present public facts", async ({ page }) => {
  await installAutomationRoutes(page, async (route) => json(route, decisionTraceEnvelope));
  await openAutomation(page);

  const timeline = decisionTimeline(page);
  await expect(timeline.getByRole("heading", { name: "Route advice" })).toBeVisible();
  await expect(timeline.getByText("Advice only — no request was executed.")).toBeVisible();
  await expect(timeline.getByText("Executed by UsageHub")).toHaveCount(2);
  await expect(timeline.getByText("primary-us")).toBeVisible();
  await expect(timeline.getByText("acct_1234567890ab")).toBeVisible();
  await expect(timeline.getByText("acct_abcdef123456")).toBeVisible();
  await expect(timeline.getByText("300 sec")).toBeVisible();
  await expect(timeline.getByText("None", { exact: true })).toBeVisible();
  await expect(timeline).not.toContainText(/prompt|response|credential|token|header|endpoint|raw error|accountId/iu);
});

test("empty runtime is not presented as an unavailable host", async ({ page }) => {
  await installAutomationRoutes(page, async (route) =>
    json(route, { apiVersion: API_VERSION, traces: [] }),
  );
  await openAutomation(page);
  await expect(
    decisionTimeline(page).locator(".decision-trace-state"),
  ).toBeVisible();
  await expect(decisionTimeline(page).locator(".decision-trace-state")).toHaveText(
    "No decisions have been recorded in this runtime yet.",
  );
  await expect(decisionTimeline(page)).not.toContainText("host may be offline");
});

test("standalone 404 is unavailable rather than an empty trace history", async ({ page }) => {
  await installAutomationRoutes(page, async (route) =>
    json(route, { error: { code: "service_unavailable" } }, 404),
  );
  await openAutomation(page);
  await expect(
    decisionTimeline(page).locator(".decision-trace-state"),
  ).toBeVisible();
  await expect(decisionTimeline(page).locator(".decision-trace-state")).toHaveText(
    "Decision Trace is unavailable. The optional Gateway host may be offline or unsupported.",
  );
  await expect(decisionTimeline(page)).not.toContainText("No decisions have been recorded");
});

test("invalid hostile projection fails closed without rendering its canary", async ({ page }) => {
  await installAutomationRoutes(page, async (route) =>
    json(route, {
      apiVersion: API_VERSION,
      traces: [],
      prompt: PRIVATE_CANARY,
    }),
  );
  await openAutomation(page);
  await expect(
    decisionTimeline(page).locator(".decision-trace-state"),
  ).toBeVisible();
  await expect(decisionTimeline(page).locator(".decision-trace-state")).toHaveText(
    "Decision Trace returned an unrecognized contract. No details are shown.",
  );
  await expect(page.getByText(PRIVATE_CANARY)).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(PRIVATE_CANARY);
});

test("keyboard refresh replaces an empty runtime with a bounded timeline", async ({ page }) => {
  let showTraces = false;
  await installAutomationRoutes(page, async (route) =>
    json(
      route,
      showTraces
        ? decisionTraceEnvelope
        : { apiVersion: API_VERSION, traces: [] },
    ),
  );
  await openAutomation(page);
  await expect(decisionTimeline(page)).toContainText(
    "No decisions have been recorded in this runtime yet.",
  );

  showTraces = true;
  const refresh = decisionTimeline(page).getByRole("button", {
    name: "Refresh decisions",
  });
  await refresh.focus();
  await expect(refresh).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    decisionTimeline(page).getByRole("heading", { name: "Pool selection" }),
  ).toBeVisible();
  await expect(refresh).toBeFocused();
});

test("initial stalled load keeps the skeleton busy until a controlled response arrives", async ({ page }) => {
  let releaseInitialLoad!: () => void;
  const initialLoadGate = new Promise<void>((resolve) => {
    releaseInitialLoad = resolve;
  });
  await installAutomationRoutes(page, async (route) => {
    await initialLoadGate;
    return json(route, { apiVersion: API_VERSION, traces: [] });
  });
  await openAutomation(page);

  const timeline = decisionTimeline(page);
  const refresh = timeline.getByRole("button", { name: "Refresh decisions" });
  await expect(timeline.locator(".decision-trace-state")).toContainText(
    "Loading recent decisions…",
  );
  await expect(timeline).toHaveAttribute("aria-busy", "true");
  await expect(refresh).toBeDisabled();
  await expect(page.locator(".service-status-announcement[role=status]")).toContainText(
    "Loading recent decisions…",
  );

  releaseInitialLoad();
  await expect(timeline).toHaveAttribute("aria-busy", "false");
  await expect(timeline).toContainText(
    "No decisions have been recorded in this runtime yet.",
  );
});

test("failed refresh keeps the last loaded timeline busy, announced, and focused", async ({ page }) => {
  let refreshing = false;
  let announceRefreshStarted!: () => void;
  let releaseRefresh!: () => void;
  const refreshStarted = new Promise<void>((resolve) => {
    announceRefreshStarted = resolve;
  });
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  await installAutomationRoutes(page, async (route) => {
    if (!refreshing) return json(route, decisionTraceEnvelope);
    announceRefreshStarted();
    await refreshGate;
    return json(route, { error: { code: "service_unavailable" } }, 503);
  });
  await openAutomation(page);

  const timeline = decisionTimeline(page);
  const routeAdvice = timeline.getByRole("heading", { name: "Route advice" });
  const refresh = timeline.getByRole("button", { name: "Refresh decisions" });
  await expect(routeAdvice).toBeVisible();
  refreshing = true;
  await refresh.focus();
  await page.keyboard.press("Enter");
  await refreshStarted;

  await expect(timeline).toHaveAttribute("aria-busy", "true");
  await expect(refresh).toBeDisabled();
  await expect(routeAdvice).toBeVisible();
  await expect(timeline).toContainText(
    "Refreshing recent decisions… Showing the last loaded decisions.",
  );
  await expect(page.locator(".service-status-announcement[role=status]")).toContainText(
    "Refreshing recent decisions… Showing the last loaded decisions.",
  );

  releaseRefresh();
  await expect(timeline).toHaveAttribute("aria-busy", "false");
  await expect(refresh).toBeEnabled();
  await expect(refresh).toBeFocused();
  await expect(routeAdvice).toBeVisible();
  await expect(timeline).toContainText(
    "Could not refresh Decision Trace. Showing the last loaded decisions.",
  );
  await expect(timeline).not.toContainText("No decisions have been recorded");
  await expect(timeline).not.toContainText("unrecognized contract");
  await expect(timeline).not.toContainText("Decision Trace is unavailable");
  await expect(page.locator(".service-status-announcement[role=status]")).toContainText(
    "Could not refresh Decision Trace. Showing the last loaded decisions.",
  );
});

test("refresh completion does not steal focus after the user chooses another control", async ({ page }) => {
  let refreshing = false;
  let announceRefreshStarted!: () => void;
  let releaseRefresh!: () => void;
  const refreshStarted = new Promise<void>((resolve) => {
    announceRefreshStarted = resolve;
  });
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  await installAutomationRoutes(page, async (route) => {
    if (!refreshing) return json(route, decisionTraceEnvelope);
    announceRefreshStarted();
    await refreshGate;
    return json(route, { error: { code: "service_unavailable" } }, 503);
  });
  await openAutomation(page);

  const timeline = page.locator(".decision-trace-card");
  const refresh = timeline.getByRole("button", { name: "Refresh decisions" });
  await expect(
    timeline.getByRole("heading", { name: "Route advice" }),
  ).toBeVisible();
  refreshing = true;
  await refresh.focus();
  await page.keyboard.press("Enter");
  await refreshStarted;
  await expect(timeline).toHaveAttribute("aria-busy", "true");

  await page.getByRole("link", { name: "Activity" }).focus();
  await page.keyboard.press("Tab");
  const userChosenControl = page.getByRole("link", { name: "Usage Details" });
  await expect(userChosenControl).toBeFocused();
  releaseRefresh();

  await expect(timeline).toHaveAttribute("aria-busy", "false");
  await expect(timeline).toContainText(
    "Could not refresh Decision Trace. Showing the last loaded decisions.",
  );
  await expect(userChosenControl).toBeFocused();
  await expect(timeline.locator("button")).not.toBeFocused();
});

for (const failure of ["invalid", "aborted"] as const) {
  test(`${failure} refresh crosses a controlled pending state before retaining the safe projection`, async ({ page }) => {
    const privateCanary = `PRIVATE_${failure.toUpperCase()}_REFRESH_CANARY_d4b1`;
    let refreshing = false;
    let announceRefreshStarted!: () => void;
    let releaseTerminal!: () => void;
    const refreshStarted = new Promise<void>((resolve) => {
      announceRefreshStarted = resolve;
    });
    const terminalGate = new Promise<void>((resolve) => {
      releaseTerminal = resolve;
    });
    await installAutomationRoutes(page, async (route) => {
      if (!refreshing) return json(route, decisionTraceEnvelope);
      announceRefreshStarted();
      await terminalGate;
      if (failure === "invalid") {
        return json(route, {
          ...decisionTraceEnvelope,
          prompt: privateCanary,
        });
      }
      return route.abort("timedout");
    });
    await openAutomation(page);

    const timeline = decisionTimeline(page);
    const routeAdvice = timeline.getByRole("heading", { name: "Route advice" });
    const refresh = timeline.getByRole("button", { name: "Refresh decisions" });
    await expect(routeAdvice).toBeVisible();
    refreshing = true;
    await refresh.focus();
    await page.keyboard.press("Enter");
    await refreshStarted;

    await expect(timeline).toHaveAttribute("aria-busy", "true");
    await expect(refresh).toBeDisabled();
    await expect(routeAdvice).toBeVisible();
    await expect(timeline).toContainText(
      "Refreshing recent decisions… Showing the last loaded decisions.",
    );
    await expect(timeline).not.toContainText(
      "Could not refresh Decision Trace. Showing the last loaded decisions.",
    );

    releaseTerminal();
    await expect(timeline).toHaveAttribute("aria-busy", "false");
    await expect(timeline).toContainText(
      "Could not refresh Decision Trace. Showing the last loaded decisions.",
    );
    await expect(routeAdvice).toBeVisible();
    await expect(refresh).toBeFocused();
    await expect(timeline).not.toContainText("No decisions have been recorded");
    await expect(timeline).not.toContainText("unrecognized contract");
    await expect(timeline).not.toContainText("Decision Trace is unavailable");
    await expect(page.locator("body")).not.toContainText(privateCanary);
  });
}

for (const viewport of [
  { width: 320, height: 720 },
  { width: 768, height: 900 },
  { width: 1440, height: 1000 },
]) {
  test(`Decision Trace fits ${viewport.width}px without horizontal overflow`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installAutomationRoutes(page, async (route) => json(route, decisionTraceEnvelope));
    await openAutomation(page);
    await expect(decisionTimeline(page).locator(".decision-trace-list")).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.innerWidth);
  });
}

async function openAutomation(page: Page) {
  await page.goto("/automation");
  await expect(
    page.getByRole("heading", { name: "Recent Decision Trace" }),
  ).toBeVisible();
}

function decisionTimeline(page: Page) {
  return page.getByRole("region", { name: "Recent Decision Trace" });
}

async function installAutomationRoutes(
  page: Page,
  decisionResponse: (route: Route) => Promise<unknown>,
) {
  await page.route("**/*", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/v1/quick-connect") return json(route, { providers: [] });
    if (pathname === "/v1/changes") {
      return json(route, { records: [], nextCursor: 0, hasMore: false });
    }
    if (pathname === "/gateway/v1/health") {
      return json(route, { error: { code: "service_unavailable" } }, 404);
    }
    if (pathname === "/gateway/v1/decision-traces") {
      await decisionResponse(route);
      return;
    }
    return route.continue();
  });
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
