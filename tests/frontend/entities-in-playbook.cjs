// Browser proof for the Entities UX redesign (sidebar nav + decluttered form).
//
// Proves end-to-end IN A REAL BROWSER against the live app:
//   0. The OLD top "Clauses | Entities" segmented toggle is GONE. Navigation lives
//      in the LEFT SIDEBAR: a Registry group ("Signing Entities") ABOVE a Clauses
//      group, and the clause heading reads "Clauses" (not "Hard Clauses").
//   1. The signing-entity registry is NOT an Admin section — it lives in the
//      Playbook editor, swapped in by the sidebar Registry nav entry.
//   2. Clicking "Signing Entities" loads the registry (the seeded entities render)
//      while the persistent sidebar stays visible.
//   3. REDESIGN: on an EXISTING entity the legal name leads as the heading; the
//      machine entity-id is NOT rendered anywhere user-visible (no caption, no
//      editable field) but the hidden id input STILL carries the persistent key;
//      the address-id field is HIDDEN (a hidden input, not a visible row).
//   4. CRUD: "Add entity" reveals the editable entity-id field (new entity only);
//      authoring + Save registry POSTs the correct {entities:[...]} payload with the
//      entity_id preserved as the persistent key (the id-round-trip invariant).
//
// The server runs on a loopback host (admin-trusted), Gmail HARD-OFF, throwaway
// data dir, on a free port (never 8787).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = process.env.PYTHON || "python3";
const PORT = Number(process.env.ENTITIES_PB_PORT || 25000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "entities-pb-data-"));
const SHOTS_DIR = process.env.ENTITIES_PB_SHOTS || DATA_DIR;

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve();
      });
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error("server did not start"));
        else setTimeout(tick, 200);
      });
    };
    tick();
  });
}

async function main() {
  assert.notEqual(PORT, 8787, "must never use 8787");
  const server = spawn(
    PYTHON,
    ["-m", "nda_automation.server", "--host", "127.0.0.1", "--port", String(PORT)],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        NDA_DATA_DIR: DATA_DIR,
        NDA_GMAIL_SYNC_ENABLED: "false",
        NDA_AI_REVIEW_ENABLED: "false",
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  server.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
  server.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

  let browser;
  try {
    await waitForServer(`${BASE_URL}/`, 20000);
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const consoleErrors = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // --- 0. The Entities admin nav section is GONE ------------------------------
    const adminEntitiesNav = await page.$$eval(
      '[data-admin-section="entities"]',
      (els) => els.length
    );
    assert.equal(adminEntitiesNav, 0, "the Admin 'Entities' nav section must be removed");

    // --- 1. Open the Playbook editor; the sidebar nav (not a toggle) is present --
    await page.click("#playbookTab");
    await page.waitForSelector("#playbookList .playbook-row");

    // The OLD top "Clauses | Entities" segmented toggle must be GONE.
    assert.equal(
      await page.$$eval(".playbook-section-switcher", (els) => els.length),
      0,
      "the top Clauses | Entities segmented toggle must be removed"
    );

    // The sidebar carries a Registry nav entry ABOVE the Clauses group.
    await page.waitForSelector("#playbookEntitiesNavEntry[data-playbook-nav='entities']");
    const railLabels = await page.$$eval(
      ".clause-rail .rail-group-label",
      (els) => els.map((el) => (el.textContent || "").trim().toLowerCase())
    );
    assert.ok(
      railLabels.indexOf("registry") !== -1 &&
        railLabels.indexOf("clauses") !== -1 &&
        railLabels.indexOf("registry") < railLabels.indexOf("clauses"),
      `Registry must appear above Clauses in the sidebar; got ${JSON.stringify(railLabels)}`
    );
    const railText = await page.$eval(".clause-rail", (el) => el.textContent || "");
    assert.ok(!/hard clauses/i.test(railText), "the sidebar must say 'Clauses', not 'Hard Clauses'");

    // The clause editor panel is visible by default; the entities panel is hidden.
    assert.equal(
      await page.$eval('[data-playbook-panel="clauses"]', (el) => el.hidden),
      false,
      "the clause editor panel should be visible by default"
    );
    assert.equal(
      await page.$eval('[data-playbook-panel="entities"]', (el) => el.hidden),
      true,
      "the entities panel should be hidden until the Registry entry is clicked"
    );

    // --- 2. Click the Registry "Signing Entities" entry -> the registry loads ----
    await page.click("#playbookEntitiesNavEntry");
    await page.waitForSelector('[data-playbook-panel="entities"]:not([hidden])');
    // The clause editor panel must be genuinely hidden (display:none).
    const clausesDisplay = await page.$eval(
      '[data-playbook-panel="clauses"]',
      (el) => getComputedStyle(el).display
    );
    assert.equal(clausesDisplay, "none", "the clause editor panel must hide when entities is active");
    // The sidebar nav stays visible (it is persistent, not a swapped surface), and
    // the Registry entry now carries the active highlight.
    const railVisible = await page.$eval(".clause-rail", (el) => el.offsetParent !== null);
    assert.ok(railVisible, "the sidebar nav must stay visible on the entities surface");
    const navActive = await page.$eval(
      "#playbookEntitiesNavEntry",
      (el) => el.classList.contains("active") || el.getAttribute("aria-pressed") === "true"
    );
    assert.ok(navActive, "the Signing Entities nav entry must show the active state");
    await page.waitForSelector("#playbookEntitiesList .entity-card");
    const cardCount = await page.$$eval("#playbookEntitiesList .entity-card", (c) => c.length);
    assert.ok(cardCount >= 1, "the seeded signing entities should render in the Playbook");
    await page.screenshot({ path: path.join(SHOTS_DIR, "01-entities-in-playbook.png"), fullPage: true });

    const firstCard = "#playbookEntitiesList .entity-card:first-child";

    // --- 3a. Existing entity: legal name leads, entity-id editable is HIDDEN -----
    const legalNameLeads = await page.$eval(
      `${firstCard} .entity-card-title strong`,
      (el) => (el.textContent || "").trim().length > 0
    );
    assert.ok(legalNameLeads, "the legal name must lead as the card heading");

    // The editable entity-id field (data-entity-new-id-field) is hidden for an
    // existing entity.
    const idFieldHidden = await page.$eval(
      `${firstCard} [data-entity-new-id-field]`,
      (el) => el.hidden
    );
    assert.equal(idFieldHidden, true, "entity-id editable field must be HIDDEN on an existing entity");

    // REDESIGN INVARIANT: the id is NOT rendered anywhere user-visible. There must be
    // no id caption element at all, and no visible text on the card containing the raw
    // id slug ("id:" or the id value itself).
    const captionCount = await page.$$eval(
      `${firstCard} [data-entity-field="id-caption"], ${firstCard} .entity-card-id`,
      (els) => els.length
    );
    assert.equal(captionCount, 0, "no id caption element must render on an existing entity card");

    // The id INPUT still carries the persistent key (hidden form state, not surfaced).
    const idValue = await page.$eval(
      `${firstCard} [data-entity-field="id"]`,
      (el) => el.value
    );
    assert.ok(idValue.length > 0, "the entity-id input must still hold the persistent key");
    // The raw id must NOT appear in any visible text on the card (the id input lives
    // inside the hidden data-entity-new-id-field label, so it never paints).
    const visibleText = await page.$eval(firstCard, (el) => (el.innerText || "").trim());
    assert.ok(
      !visibleText.includes(idValue) && !/\bid:\s*\S/i.test(visibleText),
      `the raw entity id must not render in visible card text; got: ${visibleText}`
    );

    // --- 3b. Address: id hidden, content-first (lines visible) ------------------
    await page.waitForSelector(`${firstCard} .entity-address`);
    // Address-id is a hidden input (type=hidden), never a visible labelled row.
    const addrIdHidden = await page.$eval(
      `${firstCard} .entity-address [data-address-field="id"]`,
      (el) => el.type === "hidden"
    );
    assert.ok(addrIdHidden, "the address-id must be a hidden input (not a visible field)");
    // The address lines textarea is the prominent content and holds the seeded lines.
    const linesVisible = await page.$eval(
      `${firstCard} .entity-address [data-address-field="lines"]`,
      (el) => el.offsetParent !== null && (el.value || "").length > 0
    );
    assert.ok(linesVisible, "the address lines (content) must be visible and populated");

    // --- 4. Add entity -> the editable entity-id field is REVEALED (new only) ---
    await page.click("#playbookEntitiesAddButton");
    const newCard = "#playbookEntitiesList .entity-card:last-child";
    await page.waitForSelector(`${newCard} [data-entity-new-id-field]:not([hidden])`);
    const newIdFieldShown = await page.$eval(
      `${newCard} [data-entity-new-id-field]`,
      (el) => !el.hidden
    );
    assert.ok(newIdFieldShown, "Add entity must reveal the editable entity-id field");

    // Author the new entity.
    await page.fill(`${newCard} [data-entity-field="id"]`, "test_co");
    await page.fill(`${newCard} [data-entity-field="legal_name"]`, "Test Co Limited");
    await page.fill(`${newCard} [data-entity-field="short_name"]`, "Test Co");
    await page.fill(`${newCard} [data-entity-field="jurisdiction"]`, "courts in England and Wales");
    // incorporation_jurisdiction is a required field (pre-existing validation rule).
    await page.fill(`${newCard} [data-entity-field="incorporation_jurisdiction"]`, "England and Wales");
    await page.fill(`${newCard} .entity-address [data-address-field="lines"]`, "1 Test Street\nLondon");
    // The new entity's governing law defaults to the first approved playbook option
    // (india). Forum reconciliation now validates the CANDIDATE entities being saved
    // against the playbook forum buckets, so the chosen law must agree with the court
    // text -- pick england_and_wales to match "courts in England and Wales" above.
    await page.selectOption(
      `${newCard} [data-entity-field="governing_law"]`,
      "england_and_wales"
    );

    // --- 5. Save registry -> POST carries the correct {entities:[...]} payload ---
    const saveReq = page.waitForRequest(
      (r) => r.url().includes("/api/admin/signing-entities") && r.method() === "POST"
    );
    const saveResp = page.waitForResponse(
      (r) => r.url().includes("/api/admin/signing-entities") && r.request().method() === "POST"
    );
    await page.click("#playbookEntitiesSaveButton");
    const req = await saveReq;
    const body = JSON.parse(req.postData() || "{}");
    assert.ok(Array.isArray(body.entities), "save payload must be {entities:[...]}");
    const saved = body.entities.find((e) => e.id === "test_co");
    assert.ok(saved, "the new entity must be in the save payload with its entity_id preserved");
    assert.equal(saved.legal_name, "Test Co Limited", "legal name must be in the payload");
    assert.deepEqual(
      saved.addresses[0].lines,
      ["1 Test Street", "London"],
      "address lines must be split and sent"
    );
    // The pre-existing seeded entities must still be in the payload (not dropped).
    assert.ok(
      body.entities.some((e) => e.id === idValue),
      "existing entities must be preserved in the save payload"
    );
    const resp = await saveResp;
    assert.equal(resp.status(), 200, "save registry should return 200");

    await page.screenshot({ path: path.join(SHOTS_DIR, "02-after-save.png"), fullPage: true });

    assert.deepEqual(consoleErrors, [], `no console errors expected; got ${consoleErrors.join("; ")}`);

    console.log("PASS entities-in-playbook browser proof");
    console.log(`screenshots in ${SHOTS_DIR}`);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
