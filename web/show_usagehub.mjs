import { _electron } from "playwright";
import { mkdirSync } from "node:fs";

const OUT = "/private/tmp/usagehub-shots";
mkdirSync(OUT, { recursive: true });

const app = await _electron.launch({
  executablePath: "/Users/lune/Downloads/UsageHub-0.8.6-mac-arm64/UsageHub.app/Contents/MacOS/UsageHub",
  args: [],
  timeout: 60000,
});
const page = await app.firstWindow();
await page.waitForLoadState("domcontentloaded");
await page.waitForTimeout(4000);
await page.screenshot({ path: `${OUT}/01-home.png` });

// Navigate to Providers
const providersLink = page.getByRole("link", { name: /provider|供应商/i }).first();
await providersLink.click({ timeout: 8000 }).catch(async () => {
  await page.getByText(/provider|供应商/i).first().click();
});
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/02-providers.png` });

// Open Add Provider dialog
const addBtn = page.getByRole("button", { name: /add provider|添加 provider|browse provider presets|浏览 provider 预设/i }).first();
await addBtn.click({ timeout: 8000 }).catch(async () => {
  await page.getByText(/添加|add provider/i).first().click();
});
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/03-add-provider.png` });

// Click an official preset (DeepSeek Official)
const deepseek = page.getByText(/DeepSeek/i).first();
await deepseek.click({ timeout: 8000 }).catch(() => {});
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/04-preset-form.png` });

console.log("done; url:", page.url());
await app.close();
