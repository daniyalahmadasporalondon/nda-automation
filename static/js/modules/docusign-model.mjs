// Pure DocuSign view-model: the status-mapping logic shared by the Admin
// DocuSign panel (admin-docusign.js) and the matter "Send for signature"
// action (docusign-send.js). No DOM, no fetch — just the deterministic
// decisions the frontend tests exercise:
//   - connectionView: shape the /api/docusign/status payload into a tone/label
//     for the connect/disconnect control (real OAuth, mirroring Drive/Gmail).
//   - signatureView: map a matter's /api/matters/<id>/signature-status into a
//     badge tone/label and whether the signed PDF can be downloaded.
//   - defaultSigners: the prefilled counterparty + Aspora signatory rows the
//     send chooser starts from.
//
// Exposed both as an ES module (the browser's global-bridge imports it) and as a
// CommonJS export behind the classic `typeof module` guard so the .cjs frontend
// test can require it directly — same single-source pattern as the other models.

// The envelope lifecycle the backend reports on
// GET /api/matters/<id>/signature-status. Anything outside this set is treated
// as an unknown/not-yet-sent state.
const SIGNATURE_STATUSES = ["sent", "delivered", "completed", "declined", "voided"];

const SIGNATURE_VIEW = {
  sent: { tone: "pending", label: "Awaiting signature", badge: "Sent for signature" },
  delivered: { tone: "pending", label: "Awaiting signature", badge: "Delivered to signer" },
  completed: { tone: "ready", label: "Signed", badge: "Signed" },
  declined: { tone: "blocked", label: "Declined", badge: "Signature declined" },
  voided: { tone: "blocked", label: "Voided", badge: "Envelope voided" },
};

// Normalise whatever the backend sends (case/whitespace) to one of the known
// statuses, or "" when there is no recognised envelope state.
export function normalizeSignatureStatus(value) {
  const status = String(value == null ? "" : value).trim().toLowerCase();
  return SIGNATURE_STATUSES.includes(status) ? status : "";
}

// Map a signature-status payload (or a bare status string) to the badge view.
// `null`/unknown -> the "not sent" resting state (no badge shown).
export function signatureView(statusOrPayload) {
  const raw = statusOrPayload && typeof statusOrPayload === "object"
    ? statusOrPayload.status
    : statusOrPayload;
  const status = normalizeSignatureStatus(raw);
  if (!status) {
    return {
      status: "",
      tone: "idle",
      label: "Not sent for signature",
      badge: "",
      sent: false,
      completed: false,
      canDownloadSigned: false,
      terminal: false,
    };
  }
  const view = SIGNATURE_VIEW[status];
  return {
    status,
    tone: view.tone,
    label: view.label,
    badge: view.badge,
    sent: true,
    completed: status === "completed",
    // The signed PDF is only available once the envelope is fully executed.
    canDownloadSigned: status === "completed",
    terminal: status === "completed" || status === "declined" || status === "voided",
  };
}

// The canonical signature status for a matter. The backend persists + exposes
// the envelope state NESTED under matter.docusign = {envelope_id, status, ...}
// (public_matter exposes `docusign`), which is the durable source that survives
// a reload / a freshly-fetched matter. The flat matter.signature_status only
// exists in the in-session merge after a live send/poll, so it is a fallback —
// read nested first, fall back to flat.
export function matterSignatureStatus(matter) {
  const nested = matter?.docusign;
  if (nested && typeof nested === "object" && nested.status != null && nested.status !== "") {
    return String(nested.status);
  }
  return String(matter?.signature_status || "");
}

// The canonical envelope id for a matter, with the same nested-first / flat-
// fallback rule as matterSignatureStatus.
export function matterEnvelopeId(matter) {
  const nested = matter?.docusign;
  if (nested && typeof nested === "object" && nested.envelope_id != null && nested.envelope_id !== "") {
    return String(nested.envelope_id);
  }
  return String(matter?.signature_envelope_id || "");
}

// Map a matter directly to its signature badge view, reading the canonical
// nested field (matter.docusign.status) before the in-session flat field.
export function matterSignatureView(matter) {
  return signatureView(matterSignatureStatus(matter));
}

// Shape GET /api/docusign/status -> { connected, account } into the admin
// panel's tone/label. Connect is a real DocuSign OAuth redirect (mirroring the
// Drive/Gmail Connect button): when not connected the control hands off to the
// consent flow; when connected it disconnects (removes the token).
export function connectionView(status = {}) {
  const connected = status?.connected === true;
  const account = accountLabel(status);
  const label = connected ? "Connected" : "Not connected";
  return {
    connected,
    account,
    tone: connected ? "ready" : "blocked",
    statusLabel: label,
    // The single toggle/button intent: connect when off, disconnect when on.
    actionLabel: connected ? "Disconnect DocuSign" : "Connect DocuSign",
  };
}

function accountLabel(status = {}) {
  const account = status?.account;
  if (!account) return status?.connected === true ? "Connected account" : "No account connected";
  if (typeof account === "string") return account.trim() || "Connected account";
  const name = String(account.name || account.account_name || "").trim();
  const email = String(account.email || account.account_email || "").trim();
  const id = String(account.account_id || account.id || "").trim();
  if (name && email) return `${name} (${email})`;
  return name || email || id || "Connected account";
}

// The two signer rows the send chooser opens with: the counterparty (resolved
// from the matter) signs first, then the Aspora signatory. `signing_order`
// defaults to "sequential" so signer 1 must complete before signer 2 is
// notified — matching how a finalised NDA is countersigned.
export function defaultSigners(matter = {}, options = {}) {
  // Match MatterUtils.counterpartyEmail's order: prefer the backend-derived
  // counterparty_email (the real DocuSign envelope signer, which can diverge
  // from the inbound reply recipient after a void/decline + re-route) before
  // falling back to the inbound reply/sender derivation. On a RE-SEND this keeps
  // the composer prefilled with the verified signer, not a stale reply address.
  const counterpartyEmail = String(
    matter?.counterparty_email || matter?.recipient_email || matter?.reply_to || matter?.sender || "",
  ).trim();
  const counterpartyName = String(
    matter?.counterparty_name || matter?.counterparty || matter?.document_title || "Counterparty",
  ).trim() || "Counterparty";
  const aspora = options?.asporaSignatory || {};
  const asporaName = String(aspora.name || "Aspora signatory").trim() || "Aspora signatory";
  const asporaEmail = String(aspora.email || "").trim();
  return [
    { role: "counterparty", name: counterpartyName, email: emailFromValue(counterpartyEmail), order: 1 },
    { role: "aspora", name: asporaName, email: emailFromValue(asporaEmail), order: 2 },
  ];
}

// Shape a Generator generation result into the matter-like view the send
// composer reads. The Generator's "Send for Signature" CTA acts on the freshly
// generated NDA, which is a saved matter but NOT a fetched public_matter — so we
// synthesize the few fields defaultSigners + the controller need from the
// generation result:
//   - id              -> the POST target (/api/matters/<id>/send-for-signature)
//   - counterparty/... -> the FIRST-party (counterparty) signer NAME
//   - recipient_email  -> the FIRST-party signer EMAIL
// Returns null when the generation has no saved matter id (the legacy in-memory
// blob path), so the caller keeps the CTA hidden — that NDA can't be sent.
export function generatorSignatureMatter(generated) {
  const matterId = String(generated?.matterId || "").trim();
  if (!matterId) return null;
  const name = String(generated?.counterpartyName || "").trim();
  const email = String(generated?.counterpartyEmail || "").trim();
  return {
    id: matterId,
    counterparty: name,
    counterparty_name: name,
    // Carry the signer address under both keys so this synthetic matter stays in
    // sync with the public_matter contract defaultSigners now reads
    // (counterparty_email first, recipient_email as the fallback).
    counterparty_email: email,
    recipient_email: email,
    document_title: name,
  };
}

const EMAIL_PATTERN = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;

function emailFromValue(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const bracketed = text.match(/<([^<>]+)>/);
  const candidate = bracketed?.[1] || text;
  return candidate.match(EMAIL_PATTERN)?.[0] || "";
}

// Validate the signer rows before a send: each signer needs a name and a
// syntactically valid email. Returns { ok, error, signers } where signers is
// the trimmed/normalised list the POST body should carry.
export function validateSigners(signers) {
  const rows = Array.isArray(signers) ? signers : [];
  if (!rows.length) return { ok: false, error: "Add at least one signer.", signers: [] };
  const normalised = [];
  for (const row of rows) {
    const name = String(row?.name || "").trim();
    const email = emailFromValue(row?.email);
    if (!name) return { ok: false, error: "Every signer needs a name.", signers: [] };
    if (!email) {
      return { ok: false, error: `Enter a valid email for ${name}.`, signers: [] };
    }
    normalised.push({
      role: row?.role || "",
      name,
      email,
      order: Number(row?.order) || normalised.length + 1,
    });
  }
  return { ok: true, error: "", signers: normalised };
}

// Build the POST /api/matters/<id>/send-for-signature body from the chooser.
//
// The backend treats an explicit per-signer `routing_order` as AUTHORITATIVE
// (it never overwrites it from the mode), so the FE emits it to express WHO SIGNS
// FIRST when the user reorders the rows. The mode governs what an unspecified order
// would mean, so we keep the two consistent:
//   - sequential -> carry each signer's row position as routing_order (1,2,3...),
//     so the "who signs first" reorder control actually changes DocuSign routing;
//   - parallel   -> every signer shares routing_order 1 (notify all at once), so
//     the authoritative-explicit rule still yields the parallel envelope.
// `signing_order` stays as the mode the backend also receives.
export function buildSendForSignaturePayload(signers, signingOrder = "sequential") {
  const order = signingOrder === "parallel" ? "parallel" : "sequential";
  return {
    signers: signers.map((signer, index) => ({
      name: signer.name,
      email: signer.email,
      role: signer.role,
      // Sequential: rank by chosen position (explicit order wins, else row index).
      // Parallel: collapse to a shared order 1 so the mode is honoured end-to-end.
      routing_order: order === "parallel" ? 1 : Number(signer.order) || index + 1,
    })),
    signing_order: order,
  };
}

export const DocuSignModel = {
  SIGNATURE_STATUSES,
  buildSendForSignaturePayload,
  connectionView,
  defaultSigners,
  generatorSignatureMatter,
  matterEnvelopeId,
  matterSignatureStatus,
  matterSignatureView,
  normalizeSignatureStatus,
  signatureView,
  validateSigners,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DocuSignModel,
    SIGNATURE_STATUSES,
    buildSendForSignaturePayload,
    connectionView,
    defaultSigners,
    generatorSignatureMatter,
    matterEnvelopeId,
    matterSignatureStatus,
    matterSignatureView,
    normalizeSignatureStatus,
    signatureView,
    validateSigners,
  };
}
