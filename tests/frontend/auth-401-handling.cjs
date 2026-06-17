"use strict";

// Frontend unit test for the session-expiry (401) handling fix.
//
// THE BUG: the shared `jsonRequest` helper (static/js/modules/repository-api.mjs)
// did `await response.json()` BEFORE checking `response.ok`. On session expiry
// the server returns a 401 with an empty/non-JSON body, so `response.json()`
// itself throws a SyntaxError ("The string did not match the expected pattern."
// in Safari) that masked the real cause. Worse, `loadMatters`
// (static/js/repository-actions.js) caught it and wiped the whole board.
//
// This test asserts the contract of the fix:
//   1. jsonRequest on a 401-with-non-JSON-body throws a CLEAN error with
//      `status === 401` -- NOT a SyntaxError.
//   2. The global AuthExpired handler fires on that 401 (session-expiry prompt).
//   3. loadMatters' 401 path triggers the auth-expired prompt and does NOT empty
//      state.matters or deselect the open matter.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..");
const staticDir = path.join(ROOT, "static");

// --- Shared browser-ish sandbox --------------------------------------------
// Both auth-expired.js and repository-actions.js are classic browser scripts
// (IIFEs assigning a const). We run them in one vm context that carries a
// minimal `window`/`globalThis` so the shipped wiring is exercised verbatim.

// Run a classic browser script in the sandbox. A top-level `const Foo = ...`
// does NOT attach to the sandbox global (vm semantics), so callers that need the
// declared binding pass `captureGlobal` to read it back via a trailing eval in
// the SAME context.
function loadClassicScript(relPath, sandbox, captureGlobal) {
  let code = fs.readFileSync(path.join(staticDir, relPath), "utf8");
  if (captureGlobal) code += `\n;globalThis.${captureGlobal} = ${captureGlobal};`;
  vm.runInContext(code, sandbox, { filename: relPath });
}

function makeSandbox() {
  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.console = console;
  sandbox.setTimeout = setTimeout;
  sandbox.clearTimeout = clearTimeout;
  sandbox.module = undefined; // suppress the CommonJS guard inside the scripts
  vm.createContext(sandbox);
  return sandbox;
}

// A fetch-like Response whose body is NOT valid JSON (the real session-expiry
// shape: an empty / HTML proxy page). `.json()` rejects with a SyntaxError,
// reproducing the original bug surface.
function nonJsonResponse(status) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 401 ? "Unauthorized" : "",
    async json() {
      // Mirrors what the browser does parsing an empty/HTML body.
      throw new SyntaxError("The string did not match the expected pattern.");
    },
  };
}

// The real error-shaper from app.js (kept in sync structurally).
function reviewErrorFromPayload(payload, fallbackMessage) {
  const error = new Error((payload && payload.error) || fallbackMessage);
  return error;
}

async function run() {
  let passed = 0;

  // ---------------------------------------------------------------------------
  // 1 + 2: jsonRequest on a 401 throws a clean error with status===401 and fires
  //        the global auth-expired handler -- never a SyntaxError.
  // ---------------------------------------------------------------------------
  {
    const sandbox = makeSandbox();
    loadClassicScript("js/auth-expired.js", sandbox);
    assert.ok(sandbox.AuthExpired, "AuthExpired global should be defined");

    let handledCount = 0;
    sandbox.AuthExpired.register({
      notify: () => { handledCount += 1; },
    });

    // Import the real ESM helper. globalThis.AuthExpired must be visible to it;
    // the .mjs reads it off globalThis at call time, so set the real global.
    const prevGlobal = global.AuthExpired;
    global.AuthExpired = sandbox.AuthExpired;
    try {
      const mod = await import(
        path.join(staticDir, "js", "modules", "repository-api.mjs")
      );
      const api = mod.createRepositoryApi({
        fetchImpl: async () => nonJsonResponse(401),
        reviewErrorFromPayload,
      });

      let thrown = null;
      try {
        await api.listMatters();
      } catch (error) {
        thrown = error;
      }
      assert.ok(thrown, "listMatters should reject on a 401");
      assert.ok(
        !(thrown instanceof SyntaxError),
        "the thrown error must NOT be a JSON SyntaxError",
      );
      assert.notEqual(
        thrown.message,
        "The string did not match the expected pattern.",
        "the cryptic parse message must not surface",
      );
      assert.equal(thrown.status, 401, "the thrown error must carry status 401");
      assert.equal(
        handledCount,
        1,
        "the global auth-expired handler must fire exactly once on a 401",
      );
    } finally {
      global.AuthExpired = prevGlobal;
    }
    passed += 1;
    console.log("ok 1 - jsonRequest: 401 -> clean error(status=401) + auth prompt, not SyntaxError");
  }

  // ---------------------------------------------------------------------------
  // 3: loadMatters' 401 path triggers the auth-expired prompt and does NOT wipe
  //    state.matters / deselect the open matter.
  // ---------------------------------------------------------------------------
  {
    const sandbox = makeSandbox();
    loadClassicScript("js/auth-expired.js", sandbox);
    loadClassicScript("js/repository-actions.js", sandbox, "RepositoryActions");
    assert.ok(sandbox.RepositoryActions, "RepositoryActions global should be defined");

    let promptCount = 0;
    sandbox.AuthExpired.register({ notify: () => { promptCount += 1; } });

    const existingMatters = [
      { id: "m1", counterparty: "Acme" },
      { id: "m2", counterparty: "Globex" },
    ];
    const openMatter = { id: "m1", counterparty: "Acme" };
    const state = { matters: existingMatters.slice() };
    let selectedMatter = openMatter;
    let emptyPanelRendered = false;
    let lastBoardError = null;

    // A 401 error exactly as jsonRequest now produces it.
    const authError = (() => {
      const e = new Error("Repository could not load");
      e.status = 401;
      return e;
    })();

    const actions = sandbox.RepositoryActions.create({
      api: {
        listMatters: async () => { throw authError; },
      },
      hasBoard: true,
      state,
      getSelectedMatter: () => selectedMatter,
      setSelectedMatter: (m) => { selectedMatter = m; },
      getPendingDeleteMatterId: () => null,
      setPendingDeleteMatterId: () => {},
      getPendingSendMatterId: () => null,
      setPendingSendMatterId: () => {},
      renderBoard: (opts) => { lastBoardError = opts && opts.errorMessage; },
      renderEmptyPanel: () => { emptyPanelRendered = true; },
      renderDetailPanel: () => {},
      renderSyncStatus: () => {},
    });

    await actions.loadMatters();

    assert.deepEqual(
      state.matters,
      existingMatters,
      "loadMatters must NOT empty state.matters on a 401",
    );
    assert.equal(
      selectedMatter,
      openMatter,
      "loadMatters must NOT deselect the open matter on a 401",
    );
    assert.equal(
      emptyPanelRendered,
      false,
      "loadMatters must NOT render the empty panel on a 401",
    );
    assert.ok(promptCount >= 1, "loadMatters' 401 must trigger the auth-expired prompt");
    assert.match(
      String(lastBoardError || ""),
      /session expired/i,
      "the board banner should mention the expired session",
    );
    passed += 1;
    console.log("ok 2 - loadMatters: 401 -> auth prompt, board preserved (no wipe/deselect)");
  }

  // ---------------------------------------------------------------------------
  // 4: a NON-401 failure keeps the original behaviour (wipe + empty panel), so
  //    the fix is scoped to session expiry and didn't change generic errors.
  // ---------------------------------------------------------------------------
  {
    const sandbox = makeSandbox();
    loadClassicScript("js/auth-expired.js", sandbox);
    loadClassicScript("js/repository-actions.js", sandbox, "RepositoryActions");

    const state = { matters: [{ id: "m1" }] };
    let selectedMatter = { id: "m1" };
    let emptyPanelRendered = false;

    const serverError = (() => {
      const e = new Error("Repository could not load");
      e.status = 500;
      return e;
    })();

    const actions = sandbox.RepositoryActions.create({
      api: { listMatters: async () => { throw serverError; } },
      hasBoard: true,
      state,
      getSelectedMatter: () => selectedMatter,
      setSelectedMatter: (m) => { selectedMatter = m; },
      getPendingDeleteMatterId: () => null,
      setPendingDeleteMatterId: () => {},
      getPendingSendMatterId: () => null,
      setPendingSendMatterId: () => {},
      renderBoard: () => {},
      renderEmptyPanel: () => { emptyPanelRendered = true; },
      renderDetailPanel: () => {},
      renderSyncStatus: () => {},
    });

    await actions.loadMatters();
    // Note: loadMatters reassigns state.matters with the sandbox realm's Array,
    // so assert on length rather than a cross-realm deepEqual to [].
    assert.equal(state.matters.length, 0, "a 500 still empties state.matters (unchanged behaviour)");
    assert.equal(selectedMatter, null, "a 500 still deselects the matter (unchanged behaviour)");
    assert.equal(emptyPanelRendered, true, "a 500 still renders the empty panel (unchanged behaviour)");
    passed += 1;
    console.log("ok 3 - loadMatters: 500 -> unchanged wipe behaviour (fix is 401-scoped)");
  }

  console.log(`\n# ${passed} test group(s) passed`);
}

run().then(
  () => process.exit(0),
  (error) => {
    console.error("\nFAIL:", error && error.stack ? error.stack : error);
    process.exit(1);
  },
);
