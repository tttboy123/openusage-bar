import { _electron } from "playwright";
import { mkdirSync } from "node:fs";
mkdirSync("/private/tmp/usagehub-shots", { recursive: true });

const app = await _electron.launch({
  executablePath: "/Users/lune/Downloads/UsageHub-0.8.6-mac-arm64/UsageHub.app/Contents/MacOS/UsageHub",
  args: [],
  timeout: 60000,
});
const page = await app.firstWindow();
await page.waitForLoadState("domcontentloaded");
await page.waitForTimeout(3500);
await page.getByRole("link", { name: /provider|供应商/i }).first().click({ timeout: 8000 }).catch(()=>{});
await page.waitForTimeout(2000);
await page.getByRole("button", { name: /add provider|添加 provider|browse provider presets|浏览 provider 预设/i }).first().click({ timeout: 8000 }).catch(()=>{});
await page.waitForTimeout(1200);
await page.screenshot({ path: "/private/tmp/usagehub-shots/03-add-provider.png" });
// click DeepSeek Official preset
await page.getByText("DeepSeek Official").first().click({ timeout: 8000 }).catch(()=>{});
await page.waitForTimeout(1200);
await page.screenshot({ path: "/private/tmp/usagehub-shots/04-preset-form.png" });
const form = await page.locator('[role="dialog"]').first().innerText();
console.log("=== FORM TEXT ===");
console.log(form.slice(0, 900));
await app.close();
