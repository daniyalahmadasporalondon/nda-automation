// Server-free unit checks for renderSourceAnnotations() in
// static/js/redline-rendering.js -- the read-only footnote/endnote/comment
// margin notes that surface a DOCX source's own notes and embedded Word
// comments beside the anchoring paragraph.
//
// Contracts asserted:
//   * a paragraph with no footnotes/comments renders NOTHING (an ordinary
//     agreement is byte-identical to before);
//   * a footnote reference surfaces its note text under a "Footnote N" tag;
//   * an embedded Word comment surfaces author + quoted span + comment text;
//   * all annotation HTML is contenteditable=false and escaped, so it can never
//     be typed into the paragraph body nor inject markup.
//
// Extracts the REAL production function via brace-walk (same trick as
// review-render-clobber.cjs), so no Python backend and no browser is needed.
//
// Run: node tests/frontend/source-annotations.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const RENDERING_JS = read("static/js/redline-rendering.js");

function extractFn(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) throw new Error(`could not locate function ${name}`);
  let i = source.indexOf("{", start);
  let depth = 0;
  for (; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced braces extracting ${name}`);
}

// A stand-in escapeHtml matching the app's global contract.
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// eslint-disable-next-line no-eval
const renderSourceAnnotations = eval(`(${extractFn(RENDERING_JS, "renderSourceAnnotations")})`);

// 1. Ordinary paragraph -> no annotations at all.
assert.equal(renderSourceAnnotations({ id: "p1", text: "A plain clause." }), "");
assert.equal(renderSourceAnnotations(null), "");
assert.equal(renderSourceAnnotations({ footnotes: [], comments: [] }), "");

// 2. Footnote reference surfaces its note text under a Footnote tag.
const footnoteHtml = renderSourceAnnotations({
  footnotes: [{ id: "2", kind: "footnote", offset: 17, text: "Survival is limited to trade secrets." }],
});
assert.ok(footnoteHtml.includes("Footnote 2"), "footnote tag present");
assert.ok(footnoteHtml.includes("Survival is limited to trade secrets."), "footnote text present");
assert.ok(footnoteHtml.includes('contenteditable="false"'), "footnote note is not editable");
assert.ok(footnoteHtml.includes("paragraph-source-footnote"), "footnote class present");

// Endnotes read as "Endnote N".
const endnoteHtml = renderSourceAnnotations({
  footnotes: [{ id: "5", kind: "endnote", offset: 0, text: "Residual clause." }],
});
assert.ok(endnoteHtml.includes("Endnote 5"), "endnote tag present");

// 3. Embedded Word comment surfaces author, quoted span, and comment text.
const commentHtml = renderSourceAnnotations({
  comments: [{
    id: "1",
    author: "Jane Counsel",
    quoted_text: "keep it confidential",
    text: "Please add a carve-out for compelled disclosure.",
  }],
});
assert.ok(commentHtml.includes("Comment - Jane Counsel"), "comment author tag present");
assert.ok(commentHtml.includes("keep it confidential"), "quoted span present");
assert.ok(commentHtml.includes("Please add a carve-out for compelled disclosure."), "comment text present");
assert.ok(commentHtml.includes('contenteditable="false"'), "comment note is not editable");

// 4. Markup in note/comment content is escaped, never injected.
const injectionHtml = renderSourceAnnotations({
  comments: [{ id: "9", author: "<b>X</b>", text: "<script>alert(1)</script>" }],
});
assert.ok(!injectionHtml.includes("<script>"), "comment text must be escaped");
assert.ok(!injectionHtml.includes("<b>X</b>"), "author must be escaped");
assert.ok(injectionHtml.includes("&lt;script&gt;"), "escaped script present");

console.log("source-annotations.cjs: all assertions passed");
