const { app, BrowserWindow, Tray, Menu, dialog, nativeImage } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const DASHBOARD_URL =
  process.env.USAGEHUB_DASHBOARD_URL || "http://127.0.0.1:17822";
const DASHBOARD_PORT = 17822;

let mainWindow = null;
let tray = null;
let serverProcess = null;
let isQuitting = false;

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
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "UsageHub",
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
    },
  });
  mainWindow.loadURL(DASHBOARD_URL);
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
});
