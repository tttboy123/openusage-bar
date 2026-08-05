const { app, BrowserWindow, Tray, Menu, dialog, nativeImage } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const { createReadStream, existsSync, statSync } = require("fs");
const path = require("path");

const DASHBOARD_URL =
  process.env.USAGEHUB_DASHBOARD_URL || "http://127.0.0.1:17822";
const DASHBOARD_PORT = 17822;
const WEB_ROOT = path.join(__dirname, "..", "web", "dist");

let mainWindow = null;
let tray = null;
let serverProcess = null;
let staticServer = null;
let isQuitting = false;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".woff2": "font/woff2",
};

function serveStatic(req, res) {
  let urlPath = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  if (urlPath === "/") urlPath = "/index.html";
  let file = path.join(WEB_ROOT, urlPath);
  if (!existsSync(file) || statSync(file).isDirectory()) {
    file = path.join(WEB_ROOT, "index.html");
  }
  createReadStream(file)
    .on("error", () => {
      res.statusCode = 404;
      res.end("not found");
    })
    .pipe(res);
  res.setHeader("Content-Type", MIME[path.extname(file)] ?? "application/octet-stream");
}

function proxyToDashboard(req, res) {
  const target = new URL(req.url, DASHBOARD_URL);
  const proxy = http.request(
    target,
    { method: req.method },
    (upstream) => {
      res.writeHead(upstream.statusCode ?? 500, upstream.headers);
      upstream.pipe(res);
    },
  );
  proxy.on("error", () => {
    res.statusCode = 502;
    res.end("dashboard unavailable");
  });
  req.pipe(proxy);
}

function startStaticServer() {
  return new Promise((resolve) => {
    staticServer = http.createServer((req, res) => {
      if (req.url.startsWith("/v1/")) {
        proxyToDashboard(req, res);
      } else {
        serveStatic(req, res);
      }
    });
    staticServer.listen(0, "127.0.0.1", () => resolve(staticServer));
  });
}

function dashboardUp() {
  return new Promise((resolve) => {
    const request = http.get(DASHBOARD_URL, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.setTimeout(1500, () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
  });
}

function dashboardCommand() {
  const candidates = [
    process.env.USAGEHUB_COLLECTOR,
    "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings",
  ].filter(Boolean);
  return candidates[0];
}

async function ensureDashboard() {
  if (await dashboardUp()) {
    return;
  }
  const command = dashboardCommand();
  if (!command) {
    dialog.showErrorBox(
      "UsageHub",
      "The local collector was not found. Set USAGEHUB_COLLECTOR or install the app.",
    );
    return;
  }
  serverProcess = spawn(
    command,
    ["dashboard", "--port", String(DASHBOARD_PORT)],
    { stdio: "ignore" },
  );
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (await dashboardUp()) {
      return;
    }
  }
  dialog.showErrorBox(
    "UsageHub",
    "Could not start the local dashboard server.",
  );
}

function createWindow() {
  const address = staticServer.address();
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "UsageHub",
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
}

function trayIcon() {
  const installed =
    "/Applications/OpenUsage Bar.app/Contents/Resources/icon.icns";
  const icon = nativeImage.createFromPath(installed);
  if (!icon.isEmpty()) {
    return icon;
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
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "Open UsageHub",
        click: () => {
          if (!mainWindow) {
            createWindow();
          }
          mainWindow.show();
          mainWindow.focus();
        },
      },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", () => {
    if (!mainWindow) {
      createWindow();
    }
    mainWindow.show();
    mainWindow.focus();
  });
}

app.whenReady().then(async () => {
  await ensureDashboard();
  await startStaticServer();
  createWindow();
  createTray();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    } else if (mainWindow) {
      mainWindow.show();
    }
  });
});

app.on("window-all-closed", () => {
  // Keep running in the tray on every platform; quit explicitly from the menu.
});

app.on("before-quit", () => {
  isQuitting = true;
});

app.on("quit", () => {
  if (serverProcess) {
    serverProcess.kill();
  }
  if (staticServer) {
    staticServer.close();
  }
});
