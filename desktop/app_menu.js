"use strict";

function buildReloadMenuItem() {
  return {
    label: "Reload",
    click: (_menuItem, browserWindow) => {
      browserWindow?.webContents?.reload();
    },
  };
}

module.exports = { buildReloadMenuItem };
