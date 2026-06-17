// Overview inspector tab — ESM module facade.
//
// The three Overview component renderers are authored as classic browser
// scripts (static/js/overview/{roster,footer,facts}.js): each declares its
// render entry point as a page-level global for the shell (overview-tab.js) to
// call, with a CommonJS `module.exports` guard at the bottom so Node can require
// the pure logic without a DOM. That browser-global + CJS shape is the SAME
// pattern corpus.js / dashboard-search.js use.
//
// This .mjs is the integration facade the rest of the project's module layer
// expects (mirroring static/js/modules/*.mjs): it re-exports the three render
// functions as named ESM exports so the Node frontend test
// (tests/frontend/overview-tab.mjs) — and any future ESM consumer — can import
// them through the canonical module path. It is a thin adapter: the single
// source of truth stays in the component files; nothing is re-implemented here.
//
// The component files reference `document` / `window` only lazily (inside their
// render functions), so requiring them in a DOM-less Node context is safe.

import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

const { renderOverviewRoster } = require("../overview/roster.js");
const { renderOverviewFooter } = require("../overview/footer.js");
const { renderOverviewFacts } = require("../overview/facts.js");

export { renderOverviewRoster, renderOverviewFooter, renderOverviewFacts };

// Namespace export too, so a consumer that prefers the bundled-namespace shape
// (OverviewTab.renderOverview*) — the alternative the FE test accepts — also
// resolves cleanly.
export const OverviewTab = {
  renderOverviewRoster,
  renderOverviewFooter,
  renderOverviewFacts,
};
