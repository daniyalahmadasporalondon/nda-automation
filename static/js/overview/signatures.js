// Overview tab — signature-status surface.
//
// One self-contained renderer for the SIGNATURES block in the matter-detail
// Overview tab. It paints, into a caller-supplied container, the two parties'
// e-signature state at a glance:
//
//   * A header tally — "Signatures  0/2" / "1/2" / "2/2 — Fully executed".
//   * One row per party (Aspora, Counterparty) showing Signed (with a date when
//     known) / Awaiting / Not sent.
//
// Data source: the matter's DocuSign envelope block (`matter.docusign`). After a
// signature-status sync the backend stores each recipient's per-party state on
// `docusign.signers[]` as `{ role: "aspora"|"counterparty", signature_status:
// "signed"|"awaiting"|"declined", signed_at }`. We read it from there. With no
// envelope yet (`docusign` absent / no signers) BOTH parties read "Not sent".
//
// The renderer is pure render + escape: it never fetches, never mutates app
// state, and never reaches outside `containerEl`. The shell owns the shared files
// (index.html / overview.css) and passes the matter in; we only emit the shared
// `ov-*` classes a CSS teammate styles. escapeHtml is resolved lazily via
// window.escapeHtml (same pattern as facts.js / roster.js) so the module loads
// with or without the page helper present.

const SIG_SIGNED = "signed";
const SIG_AWAITING = "awaiting";
const SIG_DECLINED = "declined";
const SIG_NOT_SENT = "not_sent";

const ASPORA_ROLE = "aspora";

// renderOverviewSignatures(containerEl, matter)
//
// `matter` is the public matter object (state.selectedMatter). We read only its
// `docusign` block; any other field is ignored. A null/blank matter or container
// is a safe no-op.
function renderOverviewSignatures(containerEl, matter) {
  if (!containerEl) return;

  const parties = signatureParties(matter || {});
  const signedCount = parties.filter((p) => p.status === SIG_SIGNED).length;
  const total = parties.length;
  const fullyExecuted = total > 0 && signedCount === total;

  // Tally line: "0/2", "1/2", "2/2 — Fully executed". When no envelope exists at
  // all (both parties not-sent) the tally still reads "0/2" but the rows carry
  // the "Not sent" state so the at-a-glance count is honest either way.
  const tally = `${signedCount}/${total}`;
  const tallyClass = fullyExecuted
    ? "ov-signatures-tally ov-signatures-tally--executed"
    : signedCount > 0
      ? "ov-signatures-tally ov-signatures-tally--partial"
      : "ov-signatures-tally";

  containerEl.innerHTML = `
    <section class="ov-signatures" aria-label="Signatures">
      <div class="ov-signatures-head">
        <span class="ov-fact-label">Signatures</span>
        <span class="${tallyClass}" data-ov-signatures-tally>${escape(tally)}${
          fullyExecuted ? ' <span class="ov-signatures-executed">Fully executed</span>' : ""
        }</span>
      </div>
      <div class="ov-signatures-rows">
        ${parties.map(renderPartyRow).join("")}
      </div>
    </section>`;
}

// Build the two-party model from the matter's DocuSign block. We ALWAYS emit
// exactly two rows in a stable order — Aspora first, then Counterparty — so the
// "x/2" tally is meaningful and the layout never jumps. Each party's status is
// resolved from the matching stored signer (by role); a missing signer or a
// matter with no envelope reads "Not sent".
function signatureParties(matter) {
  const docusign = matter && typeof matter.docusign === "object" ? matter.docusign : null;
  const signers = docusign && Array.isArray(docusign.signers) ? docusign.signers : [];
  // The envelope exists once we have an envelope_id; without it nothing was ever
  // sent, so every party reads "Not sent" regardless of any stale signer data.
  const sent = !!(docusign && String(docusign.envelope_id || "").trim());

  const aspora = signers.find((s) => s && s.role === ASPORA_ROLE) || null;
  // The counterparty is the OTHER recipient: the first non-Aspora signer.
  const counterparty = signers.find((s) => s && s.role && s.role !== ASPORA_ROLE) || null;

  return [
    partyModel("Aspora", aspora, sent),
    partyModel("Counterparty", counterparty, sent),
  ];
}

function partyModel(label, signer, sent) {
  if (!sent || !signer) {
    return { label, status: SIG_NOT_SENT, signedAt: "" };
  }
  return {
    label,
    status: normalizeStatus(signer.signature_status),
    signedAt: String(signer.signed_at || "").trim(),
  };
}

// Map the stored per-recipient status to the small UI set. The backend already
// normalizes to signed/awaiting/declined; anything else (a not-yet-synced
// envelope where signers carry no per-recipient status yet) reads "awaiting" —
// it WAS sent, we just do not yet know each party's state.
function normalizeStatus(value) {
  const v = String(value || "").trim().toLowerCase();
  if (v === SIG_SIGNED) return SIG_SIGNED;
  if (v === SIG_DECLINED) return SIG_DECLINED;
  return SIG_AWAITING;
}

function renderPartyRow(party) {
  const { label, status, signedAt } = party;
  const display = statusDisplay(status, signedAt);
  return `
    <div class="ov-signature-party" data-ov-signature-role="${escape(label.toLowerCase())}" data-ov-signature-status="${escape(status)}">
      <span class="ov-signature-party-name">${escape(label)}</span>
      <span class="ov-signature-party-status ov-signature-party-status--${escape(status)}">
        <span class="ov-signature-party-mark" aria-hidden="true">${display.mark}</span>
        <span class="ov-signature-party-text">${escape(display.text)}</span>
      </span>
    </div>`;
}

// The per-party status chip: a small mark + a label, with the signed date folded
// in when known. "Signed ✓ · 17 Jun 2026" / "Awaiting" / "Not sent" / "Declined".
function statusDisplay(status, signedAt) {
  if (status === SIG_SIGNED) {
    const when = formatSignedDate(signedAt);
    return { mark: "✓", text: when ? `Signed · ${when}` : "Signed" };
  }
  if (status === SIG_DECLINED) {
    return { mark: "✕", text: "Declined" };
  }
  if (status === SIG_NOT_SENT) {
    return { mark: "·", text: "Not sent" };
  }
  return { mark: "·", text: "Awaiting" };
}

// Signed dates arrive as a raw ISO timestamp; render date-only + human-legibly
// ("17 Jun 2026"), mirroring facts.js. Missing/unparseable -> "" so the chip
// just reads "Signed".
function formatSignedDate(value) {
  const raw = String(value == null ? "" : value).trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

// --- helpers -----------------------------------------------------------------

function escape(value) {
  return typeof window !== "undefined" && typeof window.escapeHtml === "function"
    ? window.escapeHtml(value)
    : String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Browser bridge so the shell resolves the renderer off window, plus a CommonJS
// export so the Node frontend tests can require this script directly (the export
// guard is a no-op in the browser).
if (typeof window !== "undefined") {
  window.renderOverviewSignatures = renderOverviewSignatures;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderOverviewSignatures, signatureParties };
}
