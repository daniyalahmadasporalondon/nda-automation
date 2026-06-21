// Unit proof for the FE twin of the governing-law forum carry-over fix.
//
// syncGoverningLawRules rebuilds rules.approved_options in approved_laws order,
// MERGING per-option extras (forum_jurisdiction / aliases / entity_prefixes) the
// editor has no control for onto the loaded option objects. The merge must match
// priors by STABLE (positional) identity, NOT by the id re-derived from the mutable
// label -- otherwise renaming a law (e.g. "Ontario, Canada" -> "Ontario", whose
// derived id changes ontario_canada -> ontario) silently drops its forum before the
// POST, mirroring the backend bug this branch fixes.
//
// This loads the plain (non-module) playbook-view.js via its CJS test export and
// drives the pure clause transform directly -- no DOM, no server.

const assert = require("node:assert/strict");
const path = require("node:path");

const { createPlaybookController } = require(path.resolve(
  __dirname,
  "../../static/js/playbook-view.js",
));

// Construction touches none of these deps (they are only read inside render/load
// methods we never call), so empty stubs are safe.
function makeController() {
  return createPlaybookController({
    state: {},
    playbookList: {},
    clauseDetail: {},
    renderStudioEmpty() {},
    runtime: null,
  });
}

function governingLawClauseWithForum() {
  return {
    id: "governing_law",
    type: "required",
    approved_laws: ["Ontario, Canada", "India"],
    preferred_law: "India",
    rules: {
      clause_type: "governing_law",
      approved_options: [
        {
          id: "ontario_canada",
          label: "Ontario, Canada",
          value: "Ontario, Canada",
          default: false,
          forum_jurisdiction: "Courts of Ontario, Toronto",
          aliases: ["Province of Ontario"],
        },
        {
          id: "india",
          label: "India",
          value: "India",
          default: true,
          forum_jurisdiction: "Courts of Mumbai, India",
        },
      ],
    },
  };
}

function optionsById(clause) {
  const out = {};
  for (const option of clause.rules.approved_options) out[option.id] = option;
  return out;
}

const controller = makeController();

// 1) Renaming a law's label changes its derived id but must PRESERVE its forum.
{
  const clause = governingLawClauseWithForum();
  clause.approved_laws = ["Ontario", "India"];

  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);

  assert.ok(options.ontario, "renamed option present under new id");
  assert.ok(!options.ontario_canada, "old id no longer present");
  assert.equal(
    options.ontario.forum_jurisdiction,
    "Courts of Ontario, Toronto",
    "forum_jurisdiction survives a label rename (would be dropped on base)",
  );
  assert.deepEqual(
    options.ontario.aliases,
    ["Province of Ontario"],
    "aliases survive a label rename",
  );
  assert.equal(
    options.india.forum_jurisdiction,
    "Courts of Mumbai, India",
    "unrenamed option keeps its forum",
  );
}

// 2) A plain re-sync (no rename) loses no option's forum.
{
  const clause = governingLawClauseWithForum();
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.equal(options.ontario_canada.forum_jurisdiction, "Courts of Ontario, Toronto");
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India");
}

// 3) Adding a law preserves prior forums; the brand-new option has none.
{
  const clause = governingLawClauseWithForum();
  clause.approved_laws = ["Ontario, Canada", "India", "Delaware"];
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.equal(options.ontario_canada.forum_jurisdiction, "Courts of Ontario, Toronto");
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India");
  assert.ok(options.delaware, "new option present");
  assert.equal(
    options.delaware.forum_jurisdiction,
    undefined,
    "brand-new law carries no forum (publish lint enforces one)",
  );
}

console.log("playbook-governing-law-forum.cjs: all assertions passed");
