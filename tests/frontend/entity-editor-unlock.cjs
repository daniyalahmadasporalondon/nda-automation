"use strict";

// Frontend unit test for the signing-entity editor UNLOCK (admin-entities.js).
//
// Contract under test (the FIX for the new-entity deadlock, where law/court were
// read-only on the card but required by backend validation, so a new entity
// could never be saved):
//   1. A NEW entity card renders Governing law as an editable <select> of the
//      playbook's approved options, starting on a disabled "Select governing
//      law…" placeholder (value ""), and Court/jurisdiction as an editable
//      text input (empty). Picking a law auto-suggests the matching court
//      (same coupling as the Playbook "Entities & Courts" table), and save
//      POSTs the picked values in the entityLawCourtWire shape.
//   2. An EXISTING entity card renders the SAME editable controls initialised
//      from the stored values, and an EDITED law/court round-trips through the
//      save POST. An orphan stored law (no longer an approved option) appears
//      as a disabled selected option + the inline orphan warning, forcing a
//      re-pick.
//   3. The address-lines autoGrow floor is 86px, in lockstep with the
//      .entity-address-lines { min-height: 86px } CSS and rows="4" markup.
//   4. Dirty state: pending edits relabel the Save CTA "Save changes" + add
//      .has-unsaved-changes; a successful save reverts both.
//   5. An unpicked governing law blocks save with a clear inline message (no
//      network round-trip).
//   6. "Add address" drops focus into the new row's Label input.
//
// Runs the SHIPPED controller verbatim (CommonJS export) against a jsdom DOM
// that carries the REAL card/address <template> markup extracted from
// static/index.html, so markup drift breaks this test.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const indexHtml = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const stylesCss = fs.readFileSync(path.join(ROOT, "static", "styles.css"), "utf8");
const controllerSource = fs.readFileSync(
  path.join(ROOT, "static", "js", "admin-entities.js"),
  "utf8",
);
const { createAdminEntitiesController } = require(
  path.join(ROOT, "static", "js", "admin-entities.js"),
);

const cardTplMatch = indexHtml.match(
  /<template id="adminEntityCardTemplate">[\s\S]*?<\/template>/,
);
const addrTplMatch = indexHtml.match(
  /<template id="adminEntityAddressTemplate">[\s\S]*?<\/template>/,
);
assert.ok(cardTplMatch, "adminEntityCardTemplate must exist in index.html");
assert.ok(addrTplMatch, "adminEntityAddressTemplate must exist in index.html");

const HTML = `<!doctype html><html><body>
  <section id="panel">
    <p id="message"></p>
    <div id="list"></div>
    <button id="refreshBtn" type="button">Refresh</button>
    <button id="addBtn" type="button">Add entity</button>
    <button id="saveBtn" type="button" disabled>Save registry</button>
  </section>
  ${cardTplMatch[0]}
  ${addrTplMatch[0]}
</body></html>`;

const LAW_OPTIONS = [
  {
    id: "india",
    label: "India",
    court_name: "courts in India",
    forum_jurisdiction: "India",
  },
  {
    id: "england_and_wales",
    label: "England and Wales",
    court_name: "courts in England and Wales",
    forum_jurisdiction: "England and Wales",
  },
];

function existingEntity(overrides = {}) {
  return {
    id: "aspora_uk",
    legal_name: "Aspora UK Ltd",
    short_name: "Aspora",
    jurisdiction: "courts in India",
    incorporation_jurisdiction: "India",
    governing_law: { playbook_option_id: "india", label: "India" },
    signatory: { name: "[Authorised Signatory]", title: "[Title]" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["1 Test Street", "London"],
        country: "United Kingdom",
        default: true,
      },
    ],
    ...overrides,
  };
}

function workspacePayload(entities, extra = {}) {
  return {
    entities,
    governing_law_options: LAW_OPTIONS,
    playbook_available: true,
    etag: "etag-1",
    ...extra,
  };
}

async function flush() {
  // Drain the save()'s awaited fetch/json microtasks + macrotask hops.
  for (let i = 0; i < 5; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function setup(entities, { postPayload } = {}) {
  const dom = new JSDOM(HTML, { url: "https://app.test/" });
  const doc = dom.window.document;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    calls.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    if (method === "POST") {
      const posted = JSON.parse(options.body).entities;
      const payload =
        postPayload || workspacePayload(posted, { saved: true, etag: "etag-2" });
      return { ok: true, status: 200, json: async () => payload };
    }
    return { ok: true, status: 200, json: async () => workspacePayload(entities) };
  };
  const controller = createAdminEntitiesController({
    panel: doc.querySelector("#panel"),
    list: doc.querySelector("#list"),
    message: doc.querySelector("#message"),
    refreshButton: doc.querySelector("#refreshBtn"),
    addButton: doc.querySelector("#addBtn"),
    saveButton: doc.querySelector("#saveBtn"),
    cardTemplate: doc.querySelector("#adminEntityCardTemplate"),
    addressTemplate: doc.querySelector("#adminEntityAddressTemplate"),
  });
  await controller.load();
  return { dom, doc, calls, controller };
}

function fire(dom, element, type) {
  element.dispatchEvent(new dom.window.Event(type, { bubbles: true }));
}

function lastPost(calls) {
  const posts = calls.filter((c) => c.method === "POST");
  return posts[posts.length - 1] || null;
}

async function run() {
  let passed = 0;

  // 1. NEW card: editable law select (empty placeholder default) + court input;
  //    court auto-suggest on law pick; collectEntities POSTs the wire shape.
  {
    const { dom, doc, calls } = await setup([existingEntity()]);
    doc.querySelector("#addBtn").click();
    const card = doc.querySelector("#list").lastElementChild;
    const select = card.querySelector('[data-entity-field="governing_law"]');
    assert.equal(select.tagName, "SELECT", "new card: law must be an editable <select>");
    assert.equal(select.value, "", "new card: law must default to empty (no India default)");
    const placeholder = select.options[0];
    assert.equal(placeholder.textContent, "Select governing law…");
    assert.ok(placeholder.disabled, "placeholder option must be disabled");
    assert.ok(placeholder.selected, "placeholder option must be selected");
    assert.deepEqual(
      Array.from(select.options).slice(1).map((o) => o.value),
      LAW_OPTIONS.map((o) => o.id),
      "select must offer exactly the approved playbook options",
    );
    const court = card.querySelector('[data-entity-field="jurisdiction"]');
    assert.equal(court.tagName, "INPUT", "new card: court must be an editable input");
    assert.equal(court.type, "text");
    assert.equal(court.value, "", "new card: court must default empty");

    // Pick a law -> the matching court is auto-suggested + the note shows.
    select.value = "england_and_wales";
    fire(dom, select, "change");
    assert.equal(court.value, "courts in England and Wales", "court auto-suggest on law pick");
    const note = card.querySelector('[data-entity-field="court-note"]');
    assert.ok(note && !note.hidden, "the court-updated note must be visible");

    card.querySelector('[data-entity-field="id"]').value = "test_co";
    card.querySelector('[data-entity-field="legal_name"]').value = "Test Co Limited";
    doc.querySelector("#saveBtn").click();
    await flush();
    const post = lastPost(calls);
    assert.ok(post, "save must POST");
    const saved = post.body.entities.find((e) => e.id === "test_co");
    assert.ok(saved, "the new entity must be in the POST payload");
    assert.deepEqual(
      saved.governing_law,
      { playbook_option_id: "england_and_wales", label: "England and Wales" },
      "law must POST in the entityLawCourtWire shape",
    );
    assert.equal(saved.jurisdiction, "courts in England and Wales");
    passed += 1;
  }

  // 2a. EXISTING card: editable controls initialised from stored values; an
  //     EDITED law/court round-trips through the POST.
  {
    const { dom, doc, calls } = await setup([existingEntity()]);
    const card = doc.querySelector("#list").firstElementChild;
    const select = card.querySelector('[data-entity-field="governing_law"]');
    assert.equal(select.tagName, "SELECT", "existing card: law must be editable too");
    assert.equal(select.value, "india", "select initialised to the stored option");
    assert.ok(
      !Array.from(select.options).some((o) => o.value === ""),
      "existing card with a stored law has NO empty placeholder",
    );
    const court = card.querySelector('[data-entity-field="jurisdiction"]');
    assert.equal(court.tagName, "INPUT", "existing card: court must be editable too");
    assert.equal(court.value, "courts in India", "court initialised to stored jurisdiction");

    select.value = "england_and_wales";
    fire(dom, select, "change");
    assert.equal(
      court.value,
      "courts in England and Wales",
      "law change re-suggests the matching court on an existing card",
    );
    doc.querySelector("#saveBtn").click();
    await flush();
    const saved = lastPost(calls).body.entities.find((e) => e.id === "aspora_uk");
    assert.deepEqual(saved.governing_law, {
      playbook_option_id: "england_and_wales",
      label: "England and Wales",
    });
    assert.equal(saved.jurisdiction, "courts in England and Wales");
    passed += 1;
  }

  // 2b. ORPHAN stored law: visible as a disabled selected option + inline
  //     warning, so the admin sees it and must re-pick an approved law.
  {
    const { doc } = await setup([
      existingEntity({
        id: "orphan_co",
        governing_law: { playbook_option_id: "utopia", label: "Utopia" },
        jurisdiction: "courts in Utopia",
      }),
    ]);
    const card = doc.querySelector("#list").firstElementChild;
    const select = card.querySelector('[data-entity-field="governing_law"]');
    const orphan = select.options[0];
    assert.equal(orphan.value, "utopia", "orphan law must be visible as the selected option");
    assert.ok(orphan.disabled, "orphan option must be disabled (must re-pick)");
    assert.ok(orphan.selected, "orphan option must be selected so the admin sees it");
    assert.match(orphan.textContent, /not an approved option/);
    assert.equal(select.value, "utopia");
    const warning = card.querySelector('[data-entity-field="law-warning"]');
    assert.ok(warning && !warning.hidden, "the orphan law warning must show");
    assert.match(warning.textContent, /not an approved playbook option/);
    passed += 1;
  }

  // 3. autoGrow floor is 86px, in lockstep with the CSS floor and rows="4".
  {
    const { doc } = await setup([existingEntity()]);
    const lines = doc.querySelector('[data-address-field="lines"]');
    assert.equal(lines.style.height, "86px", "autoGrow floor must be 86px (jsdom scrollHeight 0)");
    assert.match(
      controllerSource,
      /Math\.max\(textarea\.scrollHeight, 86\)/,
      "js floor must be 86",
    );
    const linesCssBlock = stylesCss.match(/\.entity-address-lines \{[\s\S]*?\}/);
    assert.ok(linesCssBlock, ".entity-address-lines rule must exist");
    assert.match(linesCssBlock[0], /min-height:\s*86px/, "css min-height must be 86px (lockstep)");
    assert.match(
      indexHtml,
      /data-address-field="lines" rows="4"/,
      "address lines textarea must start at rows=4",
    );
    passed += 1;
  }

  // 4. Dirty state: edits relabel the CTA "Save changes" + .has-unsaved-changes;
  //    a successful save reverts both.
  {
    const { dom, doc } = await setup([existingEntity()]);
    const saveBtn = doc.querySelector("#saveBtn");
    assert.equal(saveBtn.textContent, "Save registry", "at rest the CTA reads Save registry");
    assert.ok(!saveBtn.classList.contains("has-unsaved-changes"));
    const legalName = doc.querySelector('[data-entity-field="legal_name"]');
    legalName.value = "Aspora UK Limited";
    fire(dom, legalName, "input");
    assert.equal(saveBtn.textContent, "Save changes", "dirty relabels the CTA");
    assert.ok(saveBtn.classList.contains("has-unsaved-changes"), "dirty adds the marker class");
    assert.equal(saveBtn.disabled, false, "dirty must NOT change the enable/disable logic");
    saveBtn.click();
    await flush();
    assert.equal(saveBtn.textContent, "Save registry", "save success reverts the label");
    assert.ok(!saveBtn.classList.contains("has-unsaved-changes"), "save success clears the class");
    passed += 1;
  }

  // 5. An unpicked governing law blocks save with an inline message, no POST.
  {
    const { doc, calls } = await setup([existingEntity()]);
    doc.querySelector("#addBtn").click();
    const card = doc.querySelector("#list").lastElementChild;
    card.querySelector('[data-entity-field="legal_name"]').value = "No Law Co";
    doc.querySelector("#saveBtn").click();
    await flush();
    assert.equal(lastPost(calls), null, "no POST while a card has no law picked");
    const message = doc.querySelector("#message");
    assert.match(message.textContent, /Pick a governing law for No Law Co/);
    assert.ok(message.classList.contains("is-error"), "the block reads as an error");
    passed += 1;
  }

  // 6. "Add address" focuses the new row's Label input.
  {
    const { doc } = await setup([existingEntity()]);
    const card = doc.querySelector("#list").firstElementChild;
    card.querySelector("[data-entity-address-add]").click();
    const rows = card.querySelectorAll("[data-entity-address]");
    const label = rows[rows.length - 1].querySelector('[data-address-field="label"]');
    assert.equal(doc.activeElement, label, "focus must land in the new row's Label");
    passed += 1;
  }

  console.log(`PASS entity-editor-unlock (${passed} scenarios)`);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
