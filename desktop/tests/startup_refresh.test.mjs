import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const main = fs.readFileSync(new URL("../main.js", import.meta.url), "utf8");

test("desktop startup reads the daemon ledger without launching a competing refresh", () => {
  const startup = main.slice(
    main.indexOf("async function startUsageHub()"),
    main.indexOf("void startUsageHub();"),
  );

  assert.match(startup, /await refreshTraySnapshot\(\)\.catch/);
  assert.doesNotMatch(startup, /await refreshUsageData\(\)\.catch/);
});
