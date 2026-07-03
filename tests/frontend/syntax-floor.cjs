// SYNTAX-FLOOR GUARD for the shipped browser bundle.
//
// Every file under static/**/*.{js,mjs} ships to the browser UNTRANSPILED, and
// most of it loads as classic <script> tags in one chain: a single SyntaxError
// in any of them kills that whole script (app.js and everything it wires) on
// the oldest webview a legal user opens the app in. Modern syntax does not
// degrade — it detonates at parse time, before any feature detection can run.
//
// The floor this guard enforces (matching the oldest iOS webviews the app has
// been seen in — Safari 13.x era):
//
//   * Full ES2020 syntax (optional chaining `?.`, nullish `??`, dynamic
//     `import()`, BigInt, `export * as ns`) — allowed.
//   * Numeric separators (`60_000`, ES2021, Safari 13) — allowed; the tree
//     already uses them (app.js, auth-expired.js, notifications.js,
//     repository-api.mjs).
//   * Logical assignment (`??=` / `||=` / `&&=`, ES2021 but Safari 14+) —
//     REJECTED. Acorn's ecmaVersion 2021 parse accepts these, so they get a
//     dedicated AST scan; a parse-level gate alone is vacuous against the
//     single most likely modern-syntax slip.
//   * Anything ES2022+ (class fields, `#private`, class `static {}` blocks,
//     top-level await, `.at()`-era syntax additions) — REJECTED by parsing at
//     ecmaVersion 2021 exactly.
//
// sourceType is chosen by extension: `.mjs` parses as a module, `.js` as a
// classic script (matching how index.html actually loads each — `import`/
// `export` in a classic script is itself a syntax error this catches).
//
// NON-VACUITY: the self-test block below feeds the checker known-bad fixtures
// (`??=`, `||=`, `&&=`, a class field, a private field, a static block) and
// FAILS THE RUN if any of them is accepted — so the guard proves on every run
// that it still bites, not just that the tree is clean.
//
// Run: node tests/frontend/syntax-floor.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const acorn = require("acorn");

const ROOT = path.resolve(__dirname, "../..");
const STATIC_DIR = path.join(ROOT, "static");

// The parse-level floor. ES2021 (not 2020) so numeric separators — already
// shipped, Safari 13-safe — pass; the ES2021 features that are NOT Safari
// 13-safe (logical assignment) are rejected by the AST scan below instead.
const ECMA_VERSION = 2021;

// Logical-assignment operators: ES2021 syntax that needs Safari 14+, so they
// parse clean at ECMA_VERSION and must be caught by walking the AST.
const BANNED_ASSIGNMENT_OPERATORS = new Set(["??=", "||=", "&&="]);

// Minimal recursive AST walk (no acorn-walk dependency): visit every object
// node with a `type`, recursing through child nodes and node arrays.
function findBannedOperator(node) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = findBannedOperator(child);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof node.type === "string") {
    if (node.type === "AssignmentExpression" && BANNED_ASSIGNMENT_OPERATORS.has(node.operator)) {
      return node;
    }
  }
  for (const key of Object.keys(node)) {
    if (key === "loc" || key === "start" || key === "end") continue;
    const hit = findBannedOperator(node[key]);
    if (hit) return hit;
  }
  return null;
}

// Check one source text. Returns null when it fits under the floor, or a
// human-readable reason string when it does not.
function checkSource(source, { module: isModule }) {
  let ast;
  try {
    ast = acorn.parse(source, {
      ecmaVersion: ECMA_VERSION,
      sourceType: isModule ? "module" : "script",
      locations: true,
    });
  } catch (error) {
    return `does not parse at ES${ECMA_VERSION} (${isModule ? "module" : "script"}): ${error.message}`;
  }
  const banned = findBannedOperator(ast);
  if (banned) {
    const where = banned.loc ? `${banned.loc.start.line}:${banned.loc.start.column}` : "?";
    return `uses logical assignment "${banned.operator}" at ${where} (ES2021 syntax, Safari 14+; the floor is Safari 13-era webviews)`;
  }
  return null;
}

function collectBrowserSources(dir) {
  const files = [];
  (function walk(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(js|mjs)$/.test(entry.name)) files.push(full);
    }
  })(dir);
  return files.sort();
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- self-test: the guard must REJECT known over-floor fixtures ---------------
// A green run on a clean tree proves nothing unless the checker demonstrably
// still fails dirty input. Each fixture is real syntax a future edit could
// introduce; if acorn or this file regresses to accepting one, the WHOLE run
// fails before the tree scan even starts.

test("guard rejects logical assignment ??= / ||= / &&=", () => {
  for (const snippet of ["cache ??= {};", "flag ||= true;", "ready &&= verify();"]) {
    for (const module of [false, true]) {
      const reason = checkSource(snippet, { module });
      assert.ok(reason, `expected rejection of ${JSON.stringify(snippet)} (module=${module}), got clean`);
    }
  }
});

test("guard rejects ES2022+ class syntax (fields, #private, static blocks)", () => {
  const fixtures = [
    "class A { count = 0; }",
    "class A { #secret; }",
    "class A { static { init(); } }",
  ];
  for (const snippet of fixtures) {
    const reason = checkSource(snippet, { module: false });
    assert.ok(reason, `expected rejection of ${JSON.stringify(snippet)}, got clean`);
    assert.match(reason, /does not parse/, `expected a parse-level rejection for ${JSON.stringify(snippet)}`);
  }
});

test("guard rejects import/export in a classic .js script", () => {
  const reason = checkSource('import { x } from "./x.mjs";', { module: false });
  assert.ok(reason, "expected import-in-classic-script to be rejected");
});

test("guard accepts the allowed floor (?. ?? import() 60_000)", () => {
  const snippet = "const t = a?.b ?? c; const n = 60_000; import(\"./m.mjs\").then(() => {});";
  assert.equal(checkSource(snippet, { module: false }), null);
  assert.equal(checkSource(snippet, { module: true }), null);
});

// --- tree scan: every shipped browser source fits under the floor -------------

test("every static/**/*.{js,mjs} fits the ES-floor for old webviews", () => {
  const files = collectBrowserSources(STATIC_DIR);
  assert.ok(files.length > 40, `expected a real tree scan, found only ${files.length} files under static/`);
  const failures = [];
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    const reason = checkSource(source, { module: file.endsWith(".mjs") });
    if (reason) failures.push(`${path.relative(ROOT, file)}: ${reason}`);
  }
  assert.equal(
    failures.length,
    0,
    `browser sources exceed the syntax floor (one SyntaxError kills the whole classic-script chain on old iOS webviews):\n  ${failures.join("\n  ")}`,
  );
  process.stdout.write(`     scanned ${files.length} browser sources at ES${ECMA_VERSION} + logical-assignment scan\n`);
});

process.stdout.write(`syntax-floor: ${passed} checks passed\n`);
