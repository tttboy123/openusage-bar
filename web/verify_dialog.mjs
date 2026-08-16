import { _electron } from "playwright";

const app = await _electron.launch({
  executablePath: "/Users/lune/Downloads/UsageHub-0.8.6-mac-arm64/UsageHub.app/Contents/MacOS/UsageHub",
  args: [],
  timeout: 60000,
});
const page = await app.firstWindow();
await page.waitForLoadState("domcontentloaded");
await page.waitForTimeout(3500);

const providersLink = page.getByRole("link", { name: /provider|供应商/i }).first();
await providersLink.click({ timeout: 8000 }).catch(async () => {
  await page.getByText(/provider|供应商/i).first().click();
});
await page.waitForTimeout(2000);

const addBtn = page.getByRole("button", { name: /add provider|添加 provider|browse provider presets|浏览 provider 预设/i }).first();
await addBtn.click({ timeout: 8000 }).catch(async () => {
  await page.getByText(/添加|add provider/i).first().click();
});
await page.waitForTimeout(1500);

// Dialog snapshot (first ~60 lines)
const snap = await page.locator('[role="dialog"]').first().innerText();
console.log("=== DIALOG TEXT ===");
console.log(snap.slice(0, 1200));
await app.close();
