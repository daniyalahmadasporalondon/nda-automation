// Unit lock for the DEFAULT-ON faithful-DOCX render flag.
//
// faithfulDocxRenderEnabled() (static/js/docx-faithful-render.js, reading the
// FAITHFUL_DOCX_RENDER_* config from static/js/config.js) now DEFAULTS ON: the flag
// is enabled unless the localStorage value is the explicit kill-switch "false".
//
//   enabled = (value !== "false")   // case/space-insensitive
//
// Proves:
//   * absent key            -> enabled  (the new ON default).
//   * "false" (any case/pad) -> disabled (the ops/user kill-switch).
//   * legacy "1"/"true"/"on"/"yes" and even "0" -> enabled.
//   * a localStorage that throws on getItem -> falls back to the ON default.
//
// Dependency-light: loads the REAL classic scripts via vm with a tiny localStorage
// stub (no jsdom), so it runs in CI without the optional dev dependency.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

// Build a fresh sandbox with the REAL config.js + docx-faithful-render.js loaded
// over a localStorage stub seeded from `store`. Returns faithfulDocxRenderEnabled.
function enabledWith(store, { throwOnGet = false } = {}) {
  const localStorage = {
    getItem(key) {
      if (throwOnGet) throw new Error("localStorage blocked (incognito/hardened)");
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
  };
  const sandbox = { console, window: {}, localStorage };
  vm.createContext(sandbox);
  for (const file of ["config.js", "docx-faithful-render.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return vm.runInContext("faithfulDocxRenderEnabled", sandbox);
}

const KEY = "nda.faithfulDocxRender";

// Absent key -> ON (the new default).
assert.equal(enabledWith({})(), true, "absent key must default ON");

// The explicit kill-switch "false" (case/space-insensitive) -> OFF.
assert.equal(enabledWith({ [KEY]: "false" })(), false, "\"false\" must disable");
assert.equal(enabledWith({ [KEY]: "FALSE" })(), false, "\"FALSE\" must disable (case-insensitive)");
assert.equal(enabledWith({ [KEY]: " false " })(), false, "\" false \" must disable (trimmed)");

// Everything else enables -- including the legacy truthy values and even "0".
for (const value of ["1", "true", "on", "yes", "0", "", "whatever"]) {
  assert.equal(enabledWith({ [KEY]: value })(), true, `value ${JSON.stringify(value)} must enable (only \"false\" disables)`);
}

// A localStorage that throws (incognito/hardened) -> the ON default, never throws.
assert.equal(enabledWith({}, { throwOnGet: true })(), true, "a throwing localStorage must fall back to the ON default");

// The config constant itself is the ON default.
{
  const sandbox = { console, window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, "config.js"), "utf8"), sandbox, { filename: "config.js" });
  assert.equal(vm.runInContext("FAITHFUL_DOCX_RENDER_DEFAULT", sandbox), true,
    "FAITHFUL_DOCX_RENDER_DEFAULT must be true (default ON)");
}

console.log("faithful-flag-default-on: all assertions passed "
  + "(absent=ON; only \"false\" disables; legacy/0 enable; throwing localStorage -> ON).");
