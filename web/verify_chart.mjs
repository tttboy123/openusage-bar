import { _electron } from "playwright";

const app = await _electron.launch({
  executablePath: "/Users/lune/Downloads/UsageHub-0.8.6-mac-arm64/UsageHub.app/Contents/MacOS/UsageHub",
  args: [],
  timeout: 60000,
});
const page = await app.firstWindow();
await page.waitForLoadState("domcontentloaded");
await page.waitForTimeout(3500);
await page.getByRole("link", { name: /usage details|用量详情/i }).first().click({ timeout: 8000 }).catch(()=>{});
await page.waitForTimeout(3000);

const legend = await page.locator(".trend-legend-item").allInnerTexts();
const bars = await page.locator(".recharts-bar-rectangle").count();
const lines = await page.locator(".recharts-line-curve").count();
const chartSvg = await page.locator(".trend-chart svg").count();
const tipRows = await page.locator(".model-chart-tip-row").count();
console.log("legend items:", legend.length);
console.log("legend:", JSON.stringify(legend));
console.log("bar rectangles:", bars, "| line curves:", lines, "| chart svg:", chartSvg, "| tooltip rows:", tipRows);
await app.close();
