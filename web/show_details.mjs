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
// open Usage Details
const link = page.getByRole("link", { name: /usage details|用量详情/i }).first();
await link.click({ timeout: 8000 }).catch(async () => {
  await page.getByText(/用量详情|usage details/i).first().click();
});
await page.waitForTimeout(3000);
await page.screenshot({ path: "/private/tmp/usagehub-shots/05-usage-details.png", fullPage: true });
console.log("url:", page.url());
await app.close();
