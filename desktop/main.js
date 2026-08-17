const {
  ensurePackagedObserverService,
  parsePackagedLifecycleCommand,
  removePackagedObserverService,
  resolveCollectorCommand,
  resolveCollectorLifecyclePlan,
} = require("./collector_runtime");
const {
  resolveGatewayAccountEditorExecutor,
  resolveHostActionExecutor,
  resolveProviderConfigExecutor,
} = require("./settings_runtime");
const { app, BrowserWindow, Tray, Menu, dialog, nativeImage, nativeTheme, shell, globalShortcut } = require("electron");
const http = require("http");
const os = require("os");
const { existsSync } = require("fs");
const path = require("path");
const { buildProductIdentityPresentation } = require("./build_identity_copy");
const { capacityProviders } = require("./tray_state");
const { productVersionTruth } = require("./product_version_truth");
const {
  createRendererApiHandler,
  createStaticFileHandler,
  createWindowsAclVerifier,
  discoverPrivateRuntime,
  fetchPrivateObserverJson,
  isRendererApiTarget,
  probePrivateObserver,
  startOrProbePrivateObserver,
} = require("./gateway_proxy");
const {
  createPluginConnectionHandler,
  discoverPrivatePluginRuntime,
  isPluginConnectionRendererTarget,
} = require("./plugin_connection_proxy");

const WEB_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, "web", "dist")
  : path.join(__dirname, "..", "web", "dist");

let mainWindow = null;
let tray = null;
let privateObserverProcess = null;
let staticServer = null;
let isQuitting = false;
let trayUpdateTimer = null;
let lastTraySnapshot = null;
let rendererApiHandler = null;
let pluginConnectionHandler = null;
let privateRuntime = null;
let privatePluginRuntime = null;
let verifyWindowsAcl = null;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".woff2": "font/woff2",
};
const serveStatic = createStaticFileHandler({ webRoot: WEB_ROOT, mimeTypes: MIME });

function createPrivateApiHandler() {
  return createRendererApiHandler({
    runtime: privateRuntime,
    platform: process.platform,
    verifyWindowsAcl,
    gatewayAccountEditorExecutor: resolveGatewayAccountEditorExecutor({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      pathExists: existsSync,
    }),
    hostActionExecutor: resolveHostActionExecutor({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      pathExists: existsSync,
    }),
    providerConfigExecutor: resolveProviderConfigExecutor({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      pathExists: existsSync,
    }),
  });
}

function createPrivatePluginConnectionHandler() {
  return createPluginConnectionHandler({
    runtime: privatePluginRuntime,
    platform: process.platform,
    verifyWindowsAcl,
  });
}

function configurePrivateRuntime() {
  verifyWindowsAcl =
    process.platform === "win32" ? createWindowsAclVerifier() : undefined;
  try {
    privateRuntime = discoverPrivateRuntime({
      platform: process.platform,
      homeDir: os.homedir(),
      localAppData: process.env.LOCALAPPDATA,
    });
  } catch {
    privateRuntime = null;
  }
  try {
    privatePluginRuntime = discoverPrivatePluginRuntime({
      platform: process.platform,
      homeDir: os.homedir(),
      localAppData: process.env.LOCALAPPDATA,
    });
  } catch {
    privatePluginRuntime = null;
  }
}

async function ensurePrivateObserver() {
  configurePrivateRuntime();
  const lifecyclePlan = packagedCollectorLifecyclePlan();
  if (lifecyclePlan !== null) {
    const lifecycle = await ensurePackagedObserverService({
      plan: lifecyclePlan,
      probe: () => probePrivateObserver(privateRuntime, {
        platform: process.platform,
        verifyWindowsAcl,
      }),
    });
    privateObserverProcess = null;
    if (lifecycle.state === "ready") return;
    showObserverUnavailable();
    return;
  }
  if (isPackagedObserverServicePlatform()) {
    privateObserverProcess = null;
    showObserverUnavailable();
    return;
  }
  const command = dashboardCommand();
  const result = await startOrProbePrivateObserver({
    runtime: privateRuntime,
    platform: process.platform,
    command,
    verifyWindowsAcl,
  });
  privateObserverProcess = result.child;
  if (result.ready) return;
  if (privateObserverProcess) {
    privateObserverProcess.kill();
    privateObserverProcess = null;
  }
  showObserverUnavailable();
}

function isPackagedObserverServicePlatform() {
  return (
    app.isPackaged === true &&
    (process.platform === "win32" || process.platform === "linux")
  );
}

function showObserverUnavailable() {
  dialog.showErrorBox(
    "UsageHub",
    "The private Observer service is unavailable. Usage data may be temporarily unavailable.",
  );
}

function packagedCollectorLifecyclePlan() {
  return resolveCollectorLifecyclePlan({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    platform: process.platform,
    homeDir: os.homedir(),
    environment: process.env,
    pathExists: existsSync,
  });
}

async function runPackagedLifecycleCommand() {
  const request = parsePackagedLifecycleCommand({
    isPackaged: app.isPackaged,
    platform: process.platform,
    argv: process.argv.slice(1),
  });
  if (request === null) return false;
  if (request.action !== "uninstall") {
    app.exit(2);
    return true;
  }
  const plan = packagedCollectorLifecyclePlan();
  if (plan === null) {
    app.exit(3);
    return true;
  }
  const result = await removePackagedObserverService({
    plan,
    deleteData: request.deleteData,
  });
  app.exit(result.state === "removed" ? 0 : 4);
  return true;
}

function startStaticServer() {
  return new Promise((resolve) => {
    rendererApiHandler = createPrivateApiHandler();
    pluginConnectionHandler = createPrivatePluginConnectionHandler();
    staticServer = http.createServer((req, res) => {
      if (isPluginConnectionRendererTarget(req.url)) {
        pluginConnectionHandler(req, res);
      } else if (isRendererApiTarget(req.url)) {
        rendererApiHandler(req, res);
      } else {
        serveStatic(req, res);
      }
    });
    staticServer.listen(0, "127.0.0.1", () => resolve(staticServer));
  });
}

function dashboardCommand() {
  const macPath =
    "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings";
  const windowsPaths = [
    process.env.LOCALAPPDATA
      ? path.join(process.env.LOCALAPPDATA, "Programs", "OpenUsage Bar", "OpenUsage Provider Settings.exe")
      : null,
    "C:\\Program Files\\OpenUsage Bar\\OpenUsage Provider Settings.exe",
  ];
  const linuxPaths = [
    "/opt/openusage-bar/bin/openusage-provider-settings",
    path.join(os.homedir(), ".local", "bin", "openusage-provider-settings"),
  ];
  const platformPaths =
    process.platform === "darwin"
      ? [macPath]
      : process.platform === "win32"
        ? windowsPaths
        : linuxPaths;
  return resolveCollectorCommand({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    platform: process.platform,
    environment: process.env,
    pathExists: existsSync,
    developmentCandidates: platformPaths,
  });
}

function getObserverJSON(target) {
  return fetchPrivateObserverJson(target, {
    runtime: privateRuntime,
    platform: process.platform,
    verifyWindowsAcl,
  });
}

async function refreshTraySnapshot() {
  const [snapshot, capacity] = await Promise.all([
    getObserverJSON("/v1/snapshot"),
    getObserverJSON("/v1/capacity"),
  ]);
  lastTraySnapshot = { snapshot, capacity };
  updateTray();
}

function formatTokens(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  const compact = (div, suffix) =>
    `${(n / div).toFixed(1).replace(/\.0$/, "")}${suffix}`;
  if (n >= 1_000_000_000) return compact(1_000_000_000, "B");
  if (n >= 1_000_000) return compact(1_000_000, "M");
  if (n >= 1_000) return compact(1_000, "K");
  return String(n);
}

function trimNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return String(Number(n.toFixed(1)));
}

// Compact capacity unit rendering: "percent" -> "%", token/request counts use
// compact numbers, anything else keeps its unit (no "left"/verbose suffix).
function formatUnit(remaining, unit) {
  const base = trimNumber(remaining);
  const u = String(unit ?? "").toLowerCase();
  if (u === "percent" || u === "%") return `${base}%`;
  if (u === "tokens" || u === "token" || u === "requests" || u === "request") {
    return formatTokens(Number(remaining));
  }
  return `${base} ${unit ?? ""}`.trim();
}

function trimAmount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return String(Number(n.toFixed(2)));
}

const CURRENCY_SYMBOLS = {
  CNY: "¥",
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  KRW: "₩",
  HKD: "HK$",
  SGD: "S$",
  AUD: "A$",
  CAD: "C$",
};

function formatAmount(amount, currency) {
  const code = String(currency ?? "").toUpperCase();
  const symbol = CURRENCY_SYMBOLS[code] ?? `${code} `;
  return `${symbol}${trimAmount(amount)}`;
}

function trayCopy() {
  const zh = app.getLocale().toLowerCase().startsWith("zh");
  return zh
    ? {
        today: "今日 Token",
        balances: "实测余额",
        quota: "额度",
        refresh: "刷新",
        open: "打开 UsageHub",
        settings: "设置",
        quit: "退出",
      }
    : {
        today: "Today",
        balances: "Balances",
        quota: "Capacity",
        refresh: "Refresh",
        open: "Open UsageHub",
        settings: "Settings",
        quit: "Quit",
      };
}

function menuLabel() {
  const { snapshot } = lastTraySnapshot ?? {};
  const today = snapshot?.summary?.todayTokens;
  if (today == null) return "";
  return formatTokens(today);
}

function trayTitle() {
  const { snapshot, capacity } = lastTraySnapshot ?? {};
  const today = snapshot?.summary?.todayTokens;
  if (today == null) return "";
  const urgent = mostUrgent(capacity);
  const ratio = urgent?.remainingRatio ?? 1;
  const mark = ratio <= 0.2 ? "🔴" : ratio <= 0.4 ? "🟡" : "🟢";
  return `${mark} ${formatTokens(today)}`;
}

function mostUrgent(capacity) {
  const providers = capacityProviders(capacity);
  const scored = providers
    .filter((c) => c.remainingRatio != null || c.remaining != null)
    .map((c) => ({
      ...c,
      score: c.remainingRatio ?? 0,
    }))
    .sort((a, b) => a.score - b.score);
  return scored[0] ?? null;
}

function providerDisplayNames(snapshot) {
  const map = new Map();
  for (const p of Array.isArray(snapshot?.providers) ? snapshot.providers : []) {
    if (p && p.providerId) {
      map.set(p.providerId, p.displayName || p.name || p.providerId);
    }
  }
  return map;
}

function buildTrayMenu() {
  const copy = trayCopy();
  const items = [];
  const { snapshot, capacity } = lastTraySnapshot ?? {};
  const today = snapshot?.summary?.todayTokens;
  const displayNames = providerDisplayNames(snapshot);
  const displayName = (id) => displayNames.get(id) ?? id;

  items.push({
    label: today != null ? `${copy.today} ${formatTokens(today)}` : "UsageHub",
    enabled: false,
  });
  items.push({ type: "separator" });

  const balances = Array.isArray(snapshot?.balances) ? snapshot.balances : [];
  const visibleBalances = balances.filter(
    (b) => b && b.state !== "unknown" && b.available != null
  );
  if (visibleBalances.length > 0) {
    items.push({ label: copy.balances, enabled: false });
    visibleBalances.slice(0, 6).forEach((b) => {
      items.push({
        label: `${displayName(b.providerId)}  ${formatAmount(b.available, b.currency)}`,
        click: () => showMainWindow(),
      });
    });
    items.push({ type: "separator" });
  }

  const capacityItems = capacityProviders(capacity);
  if (capacityItems.length > 0) {
    items.push({ label: copy.quota, enabled: false });
    capacityItems.slice(0, 6).forEach((c) => {
      const ratio = c.remainingRatio ?? 1;
      const indicator = ratio <= 0.2 ? "🔴" : ratio <= 0.4 ? "🟡" : "🟢";
      items.push({
        label: `${indicator} ${displayName(c.providerId)} ${formatUnit(c.remaining, c.unit)}`,
        click: () => showMainWindow(),
      });
    });
    items.push({ type: "separator" });
  }

  items.push({
    label: copy.refresh,
    accelerator: "CommandOrControl+R",
    click: () => refreshTraySnapshot(),
  });
  items.push({
    label: copy.open,
    accelerator: "CommandOrControl+O",
    click: () => showMainWindow(),
  });
  items.push({
    label: copy.settings,
    accelerator: "CommandOrControl+,",
    click: () => {
      showMainWindow();
      mainWindow?.webContents?.executeJavaScript("window.location.href = '/providers'").catch(() => {});
    },
  });
  items.push({ type: "separator" });
  items.push({
    label: copy.quit,
    accelerator: "CommandOrControl+Q",
    click: () => {
      isQuitting = true;
      app.quit();
    },
  });
  return Menu.buildFromTemplate(items);
}

function updateTray() {
  if (!tray) return;
  tray.setContextMenu(buildTrayMenu());
  tray.setTitle(trayTitle());
}

function showTrayMenu() {
  if (!tray) return;
  updateTray();
  tray.popUpContextMenu();
}

function windowBackground() {
  return nativeTheme.shouldUseDarkColors ? "#0B0C0E" : "#F7F7F5";
}

function titleBarStyle() {
  return nativeTheme.shouldUseDarkColors ? "hiddenInset" : "default";
}

function createWindow() {
  const address = staticServer.address();
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "UsageHub",
    backgroundColor: windowBackground(),
    titleBarStyle: process.platform === "darwin" ? titleBarStyle() : "default",
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${address.port}/`);
  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  nativeTheme.on("updated", syncTheme);
}

function syncTheme() {
  if (!mainWindow) return;
  mainWindow.setBackgroundColor(windowBackground());
  if (process.platform === "darwin") {
    mainWindow.setTitleBarStyle(titleBarStyle());
  }
}

function showMainWindow() {
  if (!mainWindow) {
    createWindow();
  }
  mainWindow.show();
  mainWindow.focus();
}

function trayIcon() {
  // Prefer the brand template image shipped inside this app bundle
  // (Contents/Resources/trayTemplate.png, auto-loaded at @2x by Electron);
  // fall back to the bundled app icon, then the legacy native install path.
  const candidates = [
    path.join(process.resourcesPath, "trayTemplate.png"),
    path.join(process.resourcesPath, "electron.icns"),
    "/Applications/OpenUsage Bar.app/Contents/Resources/icon.icns",
  ];
  for (const candidate of candidates) {
    const icon = nativeImage.createFromPath(candidate);
    if (!icon.isEmpty()) {
      return icon;
    }
  }
  // Minimal 1x1 PNG fallback so the tray does not fail on platforms without
  // the macOS icon bundle.
  const onePixel =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
  return nativeImage.createFromDataURL(`data:image/png;base64,${onePixel}`);
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip("UsageHub");
  updateTray();
  // Left-click shows the usage summary menu; double-click opens the dashboard.
  tray.on("click", () => showTrayMenu());
  tray.on("double-click", () => showMainWindow());
}

function showAboutUsageHub() {
  const isChinese = app.getLocale().toLowerCase().startsWith("zh");
  const copy = isChinese
    ? {
        title: `关于 ${productVersionTruth.displayName}`,
        button: "好",
        versionWithBuild: "{version}（构建 {build}）",
        stageCandidate: "候选版",
        stagePrereleaseReady: "预发布就绪",
        stagePublishedPrerelease: "已发布预览版",
        publicationNotPublished: "尚未发布",
        stateUnknown: "未知",
        publishedStable: "已发布稳定版：{version}",
        canaryFormat: "Canary：{status} · {qualified}/{target}",
        canaryNotStarted: "尚未开始",
        canaryRunning: "进行中",
        canaryPassed: "已通过",
        canaryBlocked: "已阻塞",
        canaryUnknown: "未知",
      }
    : {
        title: `About ${productVersionTruth.displayName}`,
        button: "OK",
        versionWithBuild: "{version} (build {build})",
        stageCandidate: "Candidate",
        stagePrereleaseReady: "Pre-release ready",
        stagePublishedPrerelease: "Published pre-release",
        publicationNotPublished: "Not published",
        stateUnknown: "Unknown",
        publishedStable: "Published stable: {version}",
        canaryFormat: "Canary: {status} · {qualified}/{target}",
        canaryNotStarted: "Not started",
        canaryRunning: "Running",
        canaryPassed: "Passed",
        canaryBlocked: "Blocked",
        canaryUnknown: "Unknown",
      };
  const presentation = buildProductIdentityPresentation(productVersionTruth, copy);
  const detail = [
    presentation.lifecycle,
    presentation.published,
    presentation.canary,
  ].join("\n");
  void dialog.showMessageBox({
    type: "info",
    title: copy.title,
    message: `${productVersionTruth.displayName} ${presentation.versionAndBuild}`,
    detail,
    buttons: [copy.button],
    noLink: true,
  });
}

function createAppMenu() {
  const template = [
    {
      label: "UsageHub",
      submenu: [
        { label: "About UsageHub", click: showAboutUsageHub },
        { type: "separator" },
        {
          label: "Refresh",
          accelerator: "CommandOrControl+R",
          click: () => refreshTraySnapshot(),
        },
        {
          label: "Open Dashboard",
          accelerator: "CommandOrControl+O",
          click: () => showMainWindow(),
        },
        { type: "separator" },
        {
          label: "Settings",
          accelerator: "CommandOrControl+,",
          click: () => {
            showMainWindow();
            mainWindow?.webContents?.executeJavaScript("window.location.href = '/providers'").catch(() => {});
          },
        },
        { type: "separator" },
        {
          label: "Show Tray Menu",
          click: () => showTrayMenu(),
        },
        { type: "separator" },
        { label: "Quit", role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { label: "Undo", role: "undo" },
        { label: "Redo", role: "redo" },
        { type: "separator" },
        { label: "Cut", role: "cut" },
        { label: "Copy", role: "copy" },
        { label: "Paste", role: "paste" },
        { label: "Select All", role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { label: "Reload", role: "reload" },
        { label: "Force Reload", role: "forceReload" },
        { type: "separator" },
        { label: "Toggle Developer Tools", role: "toggleDevTools" },
        { type: "separator" },
        { label: "Toggle Dark Mode", click: () => nativeTheme.themeSource = nativeTheme.shouldUseDarkColors ? "light" : "dark" },
      ],
    },
    {
      label: "Window",
      submenu: [
        { label: "Minimize", role: "minimize" },
        { label: "Close", role: "close" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function startUsageHub() {
  if (await runPackagedLifecycleCommand()) return;
  app.whenReady().then(async () => {
    await ensurePrivateObserver();
    await startStaticServer();
    createAppMenu();
    createWindow();
    createTray();
    await refreshTraySnapshot();
    globalShortcut.register("CommandOrControl+Shift+T", showTrayMenu);
    trayUpdateTimer = setInterval(refreshTraySnapshot, 60_000);
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
      } else if (mainWindow) {
        mainWindow.show();
      }
    });
  });
}

void startUsageHub();

app.on("window-all-closed", () => {
  // Keep running in the tray on every platform; quit explicitly from the menu.
});

app.on("before-quit", () => {
  isQuitting = true;
});

app.on("quit", () => {
  globalShortcut.unregisterAll();
  if (trayUpdateTimer) clearInterval(trayUpdateTimer);
  if (privateObserverProcess) {
    privateObserverProcess.kill();
  }
  if (staticServer) {
    staticServer.close();
  }
});
