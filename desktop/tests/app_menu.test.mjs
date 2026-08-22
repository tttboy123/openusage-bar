import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);

test("Reload remains clickable without claiming the active refresh shortcut", () => {
  const { buildReloadMenuItem } = require("../app_menu.js");
  let reloads = 0;
  const item = buildReloadMenuItem();

  assert.equal(item.label, "Reload");
  assert.equal("role" in item, false);
  assert.equal("accelerator" in item, false);

  item.click(null, {
    webContents: {
      reload() {
        reloads += 1;
      },
    },
  });
  assert.equal(reloads, 1);
});
