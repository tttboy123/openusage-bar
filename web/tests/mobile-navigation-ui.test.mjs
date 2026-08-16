import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const appSource = fs.readFileSync(
  new URL("../src/App.tsx", import.meta.url),
  "utf8",
);
const cssSource = fs.readFileSync(
  new URL("../src/styles/app.css", import.meta.url),
  "utf8",
);
const i18nSource = fs.readFileSync(
  new URL("../src/i18n.ts", import.meta.url),
  "utf8",
);

test("the application sidebar is one localized navigation landmark", () => {
  assert.match(
    appSource,
    /<nav\s+className="sidebar"\s+aria-label=\{t\.primaryNavigation\}>/s,
  );
  assert.doesNotMatch(appSource, /<aside\s+className="sidebar"/);
  assert.equal(
    (i18nSource.match(/primaryNavigation:\s*"Primary navigation"/g) ?? [])
      .length,
    1,
  );
  assert.equal(
    (i18nSource.match(/primaryNavigation:\s*"主导航"/g) ?? []).length,
    1,
  );
});

test("route changes reveal the active mobile navigation item without forced motion", () => {
  assert.match(
    appSource,
    /const\s+activeNavRef\s*=\s*useRef<HTMLAnchorElement>\(null\)/,
  );
  assert.match(
    appSource,
    /useEffect\(\(\)\s*=>\s*\{[\s\S]*?activeNavRef\.current\?\.scrollIntoView\(\{\s*block:\s*"nearest",\s*inline:\s*"center"\s*\}\);?[\s\S]*?\},\s*\[location\.pathname\]\);/,
  );
  assert.match(
    appSource,
    /ref=\{item\.to\s*===\s*location\.pathname\s*\?\s*activeNavRef\s*:\s*undefined\}/,
  );
  assert.doesNotMatch(
    appSource,
    /scrollIntoView\(\{[^}]*behavior:\s*"smooth"/,
  );
});

test("the narrow navigation scrolls horizontally without compressing labels", () => {
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.shell\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)[^}]*align-content:\s*start[^}]*\}/i,
  );
  assert.match(
    cssSource,
    /\.sidebar\s*\{[^}]*flex-direction:\s*column/i,
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.sidebar\s*\{[^}]*overflow-x:\s*auto[^}]*scroll-snap-type:\s*x\s+proximity[^}]*\}/i,
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.nav-link\s*\{[^}]*flex:\s*0\s+0\s+auto[^}]*white-space:\s*nowrap[^}]*scroll-snap-align:\s*center[^}]*\}/i,
  );
});

test("the final mobile cascade keeps KPI values readable in two columns", () => {
  const refinedMetrics = cssSource.indexOf("/* Refined metric strips");
  const finalMobileOverrides = cssSource.lastIndexOf("@media (max-width: 640px)");

  assert.notEqual(refinedMetrics, -1);
  assert.ok(finalMobileOverrides > refinedMetrics);
  const finalMobileSource = cssSource.slice(finalMobileOverrides);
  assert.match(
    finalMobileSource,
    /\.metrics\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)[^}]*\}/i,
  );
  assert.match(
    finalMobileSource,
    /\.metric-value\s*\{[^}]*white-space:\s*normal[^}]*overflow:\s*visible[^}]*\}/i,
  );
});
