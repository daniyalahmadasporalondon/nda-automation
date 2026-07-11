// Regression lock for the inline-semantics render features (D3 hyperlinks, D4
// RTL direction) proven against the REAL shipped renderer (static/js/
// redline-rendering.js loaded via vm, exactly as the browser runs it):
//   (a) a source hyperlink run renders as an <a href> anchor with the SAME
//       display text -- the innerText (and therefore the editable round-trip to
//       paragraph.text and the outbound redline) is UNCHANGED;
//   (b) an unsafe scheme (javascript:) is NOT rendered as a live link;
//   (c) an internal #anchor hyperlink renders;
//   (d) an RTL paragraph gets dir="rtl" on its frame; an LTR one does not.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

import { escapeHtml, joinClasses, mergeClauses } from "../../static/js/modules/html-utils.mjs";
import {
  fullReplacementOperations,
  needsInlineSpace,
  renderDiffOperations,
  renderInlineToken,
} from "../../static/js/modules/inline-diff.mjs";
import { clauseStatus } from "../../static/js/modules/clause-status.mjs";
import { RedlineEditContract } from "../../static/js/modules/redline-edit-contract.mjs";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

function loadReviewDocumentRenderer() {
  const sandbox = {
    window: { RedlineEditContract },
    escapeHtml,
    joinClasses,
    mergeClauses,
    clauseStatus,
    renderDiffOperations,
    renderInlineToken,
    fullReplacementOperations,
    needsInlineSpace,
    console,
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "redline-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { renderReviewDocument: vm.runInContext("renderReviewDocument", sandbox) };
}

const { renderReviewDocument } = loadReviewDocumentRenderer();

// innerText of an HTML fragment: strip tags and decode the few entities escapeHtml
// emits. This is what a contenteditable body round-trips back to paragraph.text.
function innerText(html) {
  return html
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function renderDoc(paragraphs) {
  return renderReviewDocument({
    clauses: [],
    originalParagraphs: paragraphs,
    paragraphs,
    comments: [],
    redlines: [],
    selectedClauseId: null,
    viewMode: "redline",
  });
}

// (a) Hyperlink run -> <a href>, display text unchanged.
const linkText = "See the privacy policy for details.";
const linkParagraph = {
  id: "p_link",
  text: linkText,
  runs: [
    { text: "See the ", bold: false, italic: false, underline: false },
    { text: "privacy policy", bold: false, italic: false, underline: true, hyperlink: "https://example.com/privacy" },
    { text: " for details.", bold: false, italic: false, underline: false },
  ],
};
const linkHtml = renderDoc([linkParagraph]);
assert.ok(
  linkHtml.includes('<a href="https://example.com/privacy"'),
  "hyperlink: the run renders as an anchor to the captured target",
);
assert.ok(linkHtml.includes('class="doc-run-link"'), "hyperlink: anchor carries the doc-run-link class");
assert.ok(linkHtml.includes(">privacy policy<"), "hyperlink: the anchor wraps the SAME display text");
// The critical invariant: innerText is byte-identical to paragraph.text.
const linkBody = linkHtml.slice(linkHtml.indexOf('data-paragraph-id="p_link"'));
assert.ok(innerText(linkBody).includes(linkText), "hyperlink: innerText is unchanged (outbound redline safe)");

// (b) Unsafe scheme is NOT rendered as a live link.
const evilParagraph = {
  id: "p_evil",
  text: "Click here now.",
  runs: [
    { text: "Click ", bold: false, italic: false, underline: false },
    { text: "here", bold: false, italic: false, underline: true, hyperlink: "javascript:alert(1)" },
    { text: " now.", bold: false, italic: false, underline: false },
  ],
};
const evilHtml = renderDoc([evilParagraph]);
assert.ok(!/javascript:/i.test(evilHtml), "unsafe scheme: javascript: target is never rendered");
assert.ok(!evilHtml.includes('<a '), "unsafe scheme: no anchor element is produced for a disallowed target");
assert.ok(evilHtml.includes("here"), "unsafe scheme: the display text still renders as plain text");

// (c) Internal #anchor hyperlink renders.
const anchorParagraph = {
  id: "p_anchor",
  text: "Go to Section 2.",
  runs: [
    { text: "Go to ", bold: false, italic: false, underline: false },
    { text: "Section 2", bold: false, italic: false, underline: true, hyperlink: "#Section2" },
    { text: ".", bold: false, italic: false, underline: false },
  ],
};
const anchorHtml = renderDoc([anchorParagraph]);
assert.ok(anchorHtml.includes('<a href="#Section2"'), "anchor: an internal #anchor link renders");

// (d) RTL direction -> dir="rtl" on the frame; LTR paragraph unaffected.
const rtlHtml = renderDoc([
  { id: "p_rtl", text: "Right-to-left clause.", direction: "rtl" },
  { id: "p_ltr", text: "Ordinary clause." },
]);
const rtlFrame = rtlHtml.slice(rtlHtml.indexOf('data-paragraph-id="p_rtl"'), rtlHtml.indexOf('data-paragraph-id="p_ltr"'));
const ltrFrame = rtlHtml.slice(rtlHtml.indexOf('data-paragraph-id="p_ltr"'));
assert.ok(rtlFrame.includes('dir="rtl"'), "rtl: the RTL paragraph frame carries dir=rtl");
assert.ok(!ltrFrame.includes('dir="rtl"'), "rtl: the LTR paragraph frame carries no dir attribute");

console.log("inline-semantics-render.mjs: all assertions passed");
