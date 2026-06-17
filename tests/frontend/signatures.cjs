"use strict";

// Frontend unit test for the Overview tab SIGNATURES block.
//
// static/js/overview/signatures.js is a classic browser script exposing its
// renderer + the pure party-model helper behind a `typeof module` CommonJS guard
// (a no-op in the browser, same pattern as roster.js / facts.js). We require it
// here and exercise the render path against a tiny innerHTML-capturing container
// stub — no jsdom, matching the repo's zero-dep FE harness style.
//
// Covers the four product cases: no envelope -> 0/2 "Not sent"; one party signed
// -> 1/2 (and WHICH party); both signed -> 2/2 "Fully executed"; and that the
// Aspora vs counterparty mapping is driven by the stored signer ROLE.

const assert = require("node:assert/strict");
const path = require("node:path");

const { renderOverviewSignatures, signatureParties } = require(
  path.join(__dirname, "..", "..", "static", "js", "overview", "signatures.js"),
);

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

function makeContainer() {
  return { innerHTML: "" };
}

// --- pure model --------------------------------------------------------------

test("signatureParties always returns Aspora then Counterparty", () => {
  const parties = signatureParties({});
  assert.equal(parties.length, 2);
  assert.equal(parties[0].label, "Aspora");
  assert.equal(parties[1].label, "Counterparty");
});

test("no envelope -> both parties not_sent", () => {
  const parties = signatureParties({ docusign: {} });
  assert.equal(parties[0].status, "not_sent");
  assert.equal(parties[1].status, "not_sent");
});

test("envelope sent, neither signed -> both awaiting", () => {
  const matter = {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "daniyal.ahmad@aspora.com", signature_status: "awaiting" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "awaiting" },
      ],
    },
  };
  const parties = signatureParties(matter);
  assert.equal(parties[0].status, "awaiting");
  assert.equal(parties[1].status, "awaiting");
});

test("only Aspora signed -> Aspora signed, Counterparty awaiting (which party is clear)", () => {
  const matter = {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "daniyal.ahmad@aspora.com", signature_status: "signed", signed_at: "2026-06-17T10:00:00+00:00" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "awaiting" },
      ],
    },
  };
  const parties = signatureParties(matter);
  assert.equal(parties[0].status, "signed");
  assert.equal(parties[0].signedAt, "2026-06-17T10:00:00+00:00");
  assert.equal(parties[1].status, "awaiting");
});

test("only Counterparty signed -> Counterparty signed, Aspora awaiting", () => {
  const matter = {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "daniyal.ahmad@aspora.com", signature_status: "awaiting" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "signed" },
      ],
    },
  };
  const parties = signatureParties(matter);
  assert.equal(parties[0].status, "awaiting");
  assert.equal(parties[1].status, "signed");
});

test("both signed -> both signed (2/2)", () => {
  const matter = {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "a@aspora.com", signature_status: "signed" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "signed" },
      ],
    },
  };
  const parties = signatureParties(matter);
  assert.ok(parties.every((p) => p.status === "signed"));
});

test("missing per-recipient status on a sent envelope reads awaiting (was sent)", () => {
  const matter = {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "a@aspora.com" },
        { role: "counterparty", email: "cp@acme.com" },
      ],
    },
  };
  const parties = signatureParties(matter);
  assert.equal(parties[0].status, "awaiting");
  assert.equal(parties[1].status, "awaiting");
});

// --- render path -------------------------------------------------------------

test("render is a safe no-op without a container", () => {
  assert.doesNotThrow(() => renderOverviewSignatures(null, {}));
});

test("render 0/2 with not-sent rows when no envelope", () => {
  const c = makeContainer();
  renderOverviewSignatures(c, {});
  assert.match(c.innerHTML, /data-ov-signatures-tally[^>]*>0\/2/);
  assert.match(c.innerHTML, /Not sent/);
  // Two party rows, both not_sent.
  const rows = c.innerHTML.match(/ov-signature-party"/g) || [];
  assert.equal(rows.length, 2);
  assert.match(c.innerHTML, /data-ov-signature-status="not_sent"/);
});

test("render 1/2 shows the tally and which party signed", () => {
  const c = makeContainer();
  renderOverviewSignatures(c, {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "a@aspora.com", signature_status: "signed", signed_at: "2026-06-17T10:00:00+00:00" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "awaiting" },
      ],
    },
  });
  assert.match(c.innerHTML, /data-ov-signatures-tally[^>]*>1\/2/);
  // Aspora row signed, counterparty row awaiting.
  assert.match(c.innerHTML, /data-ov-signature-role="aspora"[^>]*data-ov-signature-status="signed"/);
  assert.match(c.innerHTML, /data-ov-signature-role="counterparty"[^>]*data-ov-signature-status="awaiting"/);
  // The signed date is rendered into the chip.
  assert.match(c.innerHTML, /17 Jun 2026/);
});

test("render 2/2 shows the fully-executed marker", () => {
  const c = makeContainer();
  renderOverviewSignatures(c, {
    docusign: {
      envelope_id: "env-1",
      signers: [
        { role: "aspora", email: "a@aspora.com", signature_status: "signed" },
        { role: "counterparty", email: "cp@acme.com", signature_status: "signed" },
      ],
    },
  });
  assert.match(c.innerHTML, /ov-signatures-tally--executed[^>]*data-ov-signatures-tally/);
  assert.match(c.innerHTML, /2\/2/);
  assert.match(c.innerHTML, /Fully executed/);
});

test("render escapes a hostile signed_at / role without breaking attributes", () => {
  const c = makeContainer();
  assert.doesNotThrow(() =>
    renderOverviewSignatures(c, {
      docusign: {
        envelope_id: "env-1",
        signers: [
          { role: "aspora", email: "a@aspora.com", signature_status: "signed", signed_at: '"><img src=x>' },
          { role: "counterparty", email: "cp@acme.com", signature_status: "awaiting" },
        ],
      },
    }),
  );
  assert.doesNotMatch(c.innerHTML, /<img src=x>/);
});

process.stdout.write(`\n${passed} passed\n`);
