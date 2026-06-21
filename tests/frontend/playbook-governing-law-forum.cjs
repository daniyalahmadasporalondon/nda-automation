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

// Three approved laws, each with a DISTINCT forum, so a cross-wire is detectable.
function governingLawClauseThreeForums() {
  return {
    id: "governing_law",
    type: "required",
    approved_laws: ["India", "Delaware", "England and Wales"],
    preferred_law: "India",
    rules: {
      clause_type: "governing_law",
      approved_options: [
        { id: "india", label: "India", value: "India", default: true, forum_jurisdiction: "Courts of Mumbai, India" },
        { id: "delaware", label: "Delaware", value: "Delaware", default: false, forum_jurisdiction: "Courts of Delaware, USA" },
        {
          id: "england_and_wales",
          label: "England and Wales",
          value: "England and Wales",
          default: false,
          forum_jurisdiction: "Courts of England and Wales, London",
        },
      ],
    },
  };
}

// 4) REORDER/swap: each law keeps ITS OWN forum (position-first would swap them).
{
  const clause = governingLawClauseThreeForums();
  clause.approved_laws = ["England and Wales", "Delaware", "India"];
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India", "reorder: India keeps own forum");
  assert.equal(options.delaware.forum_jurisdiction, "Courts of Delaware, USA", "reorder: Delaware keeps own forum");
  assert.equal(
    options.england_and_wales.forum_jurisdiction,
    "Courts of England and Wales, London",
    "reorder: England keeps own forum",
  );
}

// 5) INSERT mid-list: new law gets NO stale forum; the others keep theirs.
{
  const clause = governingLawClauseThreeForums();
  clause.approved_laws = ["India", "Singapore", "Delaware", "England and Wales"];
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.ok(options.singapore, "inserted option present");
  assert.equal(
    options.singapore.forum_jurisdiction,
    undefined,
    "inserted law must not inherit a shifted neighbour's forum",
  );
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India");
  assert.equal(options.delaware.forum_jurisdiction, "Courts of Delaware, USA");
  assert.equal(options.england_and_wales.forum_jurisdiction, "Courts of England and Wales, London");
}

// 6) DELETE mid-list: surviving laws keep their OWN forums (the P0 cross-wire).
{
  const clause = governingLawClauseThreeForums();
  clause.approved_laws = ["India", "England and Wales"]; // drop Delaware
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.ok(!options.delaware, "deleted option gone");
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India");
  assert.equal(
    options.england_and_wales.forum_jurisdiction,
    "Courts of England and Wales, London",
    "delete-mid must not shift a neighbour's forum onto England",
  );
}

// 7) RENAME + REORDER: renamed law does not steal a still-present law's forum.
{
  const clause = governingLawClauseThreeForums();
  // Rename Delaware -> "Delaware, USA" (id delaware -> delaware_usa) AND move it to slot 0.
  clause.approved_laws = ["Delaware, USA", "India", "England and Wales"];
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  // delaware_usa's slot-0 prior is India, still present + id-claimed -> no fallback.
  assert.equal(
    options.delaware_usa.forum_jurisdiction,
    undefined,
    "renamed+reordered law must not steal India's forum via its old slot",
  );
  assert.equal(options.india.forum_jurisdiction, "Courts of Mumbai, India");
  assert.equal(options.england_and_wales.forum_jurisdiction, "Courts of England and Wales, London");
  // India's forum is held by exactly one option.
  const indiaHolders = Object.values(options).filter(
    (opt) => opt.forum_jurisdiction === "Courts of Mumbai, India",
  );
  assert.equal(indiaHolders.length, 1, "India's forum must not be duplicated onto two options");
}

// A forum-BEARING law before a forum-LESS survivor. Deleting the bearing law must
// not graft its court onto the survivor. The BACKEND grafted this on 886a09f3; the
// FE was already correct -- these lock the FE side AND document the shared (no-graft)
// contract the backend now matches (see test_playbook_rules.py mirrors). The
// expected output below is IDENTICAL on FE and BE: survivor has no forum_jurisdiction.
function governingLawClauseForumThenForumless() {
  return {
    id: "governing_law",
    type: "required",
    approved_laws: ["India", "NewLaw"],
    preferred_law: "India",
    rules: {
      clause_type: "governing_law",
      approved_options: [
        { id: "india", label: "India", value: "India", default: true, forum_jurisdiction: "Courts of Mumbai, India" },
        { id: "newlaw", label: "NewLaw", value: "NewLaw", default: false }, // no forum authored
      ],
    },
  };
}

// 8) DELETE a forum-bearing law before a forum-less survivor -> survivor keeps NONE.
{
  const clause = governingLawClauseForumThenForumless();
  clause.approved_laws = ["NewLaw"]; // drop India
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.ok(!options.india, "deleted forum-bearing law gone");
  assert.ok(options.newlaw, "survivor present");
  assert.equal(
    options.newlaw.forum_jurisdiction,
    undefined,
    "forum-less survivor must not inherit the deleted neighbour's court (FE==BE)",
  );
}

// 9) Same delete WITH a simultaneous rename of the survivor -> still NO graft.
{
  const clause = governingLawClauseForumThenForumless();
  clause.approved_laws = ["New Law plc"]; // drop India AND rename NewLaw -> new_law_plc
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.ok(!options.india, "deleted forum-bearing law gone");
  assert.ok(options.new_law_plc, "renamed survivor present");
  assert.equal(
    options.new_law_plc.forum_jurisdiction,
    undefined,
    "renamed forum-less survivor must not inherit the deleted law's court (FE==BE)",
  );
}

// 10) TWO laws renamed AND swapped in one same-cardinality edit -> ambiguous, so
// NEITHER renamed law may take the position fallback: each gets NO forum (not the
// swapped neighbour's court). The untouched law keeps its own. Identical FE/BE.
{
  const clause = governingLawClauseThreeForums();
  // [India, England and Wales, Delaware] -> [England, Bharat, Delaware]
  clause.approved_laws = ["England", "Bharat", "Delaware"];
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.equal(
    options.england.forum_jurisdiction,
    undefined,
    "ambiguous double-rename must not graft a neighbour's court (FE==BE)",
  );
  assert.equal(
    options.bharat.forum_jurisdiction,
    undefined,
    "ambiguous double-rename must not graft a neighbour's court (FE==BE)",
  );
  assert.equal(options.delaware.forum_jurisdiction, "Courts of Delaware, USA");
  const forums = new Set(Object.values(options).map((o) => o.forum_jurisdiction));
  assert.ok(!forums.has("Courts of Mumbai, India"), "India's court must not leak");
  assert.ok(!forums.has("Courts of England and Wales, London"), "England's court must not leak");
}

// 11) SINGLE pure rename still keeps its forum (don't re-break the original bug).
{
  const clause = governingLawClauseThreeForums();
  clause.approved_laws = ["Bharat", "England and Wales", "Delaware"]; // India -> Bharat only
  controller.syncGoverningLawRules(clause);
  const options = optionsById(clause);
  assert.equal(
    options.bharat.forum_jurisdiction,
    "Courts of Mumbai, India",
    "a single pure rename must preserve the renamed law's forum (FE==BE)",
  );
  assert.equal(options.england_and_wales.forum_jurisdiction, "Courts of England and Wales, London");
  assert.equal(options.delaware.forum_jurisdiction, "Courts of Delaware, USA");
}

console.log("playbook-governing-law-forum.cjs: all assertions passed");
