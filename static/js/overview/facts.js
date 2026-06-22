// Overview tab — counterparty + matter-facts surface.
//
// One self-contained renderer for the new "Overview" inspector tab. It paints two
// things into a caller-supplied container:
//
//   1. A COUNTERPARTY block (`ov-counterparty`): the extracted counterparty name,
//      read as "Counterparty: <Name>", with its confirmed / unconfirmed state.
//      When the counterparty still needs human confirmation it shows an
//      "Unconfirmed" badge and a "Confirm" control (-> onConfirm()); this mirrors
//      the existing review-workstation counterparty field
//      (matter.counterparty_needs_confirmation !== false drives the badge).
//
//      The name is NOT a free-text input. To change it the user clicks a small
//      "Edit" button next to the name, which reveals an inline edit field (the
//      name itself becomes editable on demand) with Save / Cancel; saving commits
//      the trimmed value via onEntityFill(value) — the SAME write path the old
//      always-on "Entity name" input used. Edit changes the name; Confirm confirms
//      it. Below the name, the inbound SENDER's email is shown as a labelled fact
//      line (hidden gracefully when the matter has no sender, e.g. manual upload).
//
//   2. A matter-FACTS strip (`ov-facts`) of `ov-fact` items for the received
//      date. Governing law and term were REMOVED from this strip: they duplicate
//      the "Governing Law" and "Term and Survival" clauses already shown in the
//      roster, which confused reviewers. The received date is the only fact left,
//      rendered date-only and human-legibly (e.g. "17 Jun 2026") rather than as a
//      raw ISO timestamp.
//
// The shell owns the shared files (index.html / styles.css) and wires the two
// callbacks to the app's existing confirm + entity-fill logic, so this module is
// pure render + event-dispatch: it never fetches, never mutates app state, and
// never reaches outside `containerEl`. A CSS teammate styles the shared classes
// (`ov-counterparty`, `ov-facts`, `ov-fact`); we only emit them.
//
// The sender email arrives on the data object as `sender` (the raw inbound Gmail
// "From" value, e.g. '"Legal" <legal@acme.com>') — the same matter.sender field
// the repository board/inbox surface. It is rendered, when present, as a labelled
// "SENDER" line directly under the counterparty row.
//
// escapeHtml is resolved lazily via window.escapeHtml (same pattern as the Fill
// tab) so the module loads with or without the page's helper present.

function renderOverviewFacts(containerEl, data, handlers) {
  if (!containerEl) return;

  const model = data || {};
  // The counterparty arrives either as the shell's {name, confirmed} object or,
  // from a leaner caller, as a bare name string. Normalize both to {name,
  // confirmed} so this renderer accepts either contract.
  const counterparty =
    typeof model.counterparty === "string"
      ? { name: model.counterparty, confirmed: false }
      : model.counterparty || {};
  // Facts keys are accepted in either the shell's camelCase (receivedDate) or a
  // snake_case/short form (received_at) so the renderer is tolerant of either
  // data shape. (Governing law + term were removed — they duplicate roster
  // clauses; see normalizeFacts.)
  const facts = normalizeFacts(model.facts || {});
  const callbacks = handlers || {};
  const onConfirm = typeof callbacks.onConfirm === "function" ? callbacks.onConfirm : null;
  const onEntityFill = typeof callbacks.onEntityFill === "function" ? callbacks.onEntityFill : null;

  const name = String(counterparty.name == null ? "" : counterparty.name).trim();
  // Trust the caller's confirmed flag; absent / non-true reads as unconfirmed so
  // the Confirm control fails open the same way the backend badge decision does.
  const confirmed = counterparty.confirmed === true;

  // Inbound SENDER email (matter.sender). Accepted at the top level (`sender`) or
  // nested in the raw facts object (`facts.sender`) so the renderer tolerates
  // either caller shape. Empty / missing -> the SENDER line is omitted entirely
  // (manual uploads carry no sender).
  const rawFacts = model.facts || {};
  const sender = String(
    model.sender != null ? model.sender : rawFacts.sender != null ? rawFacts.sender : "",
  ).trim();

  containerEl.innerHTML = `
    ${renderCounterparty(name, confirmed, sender)}
    ${renderFacts(facts)}
  `;

  bindEvents(containerEl, onConfirm, onEntityFill);
}

// --- counterparty block ------------------------------------------------------

function renderCounterparty(name, confirmed, sender) {
  // "Counterparty: <Name>" — a label + the name on one line, NOT a free-text
  // input. The name reads as text; an "Edit" button next to it opens the inline
  // editor on demand.
  const displayName = name
    ? `<span class="ov-counterparty-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>`
    : '<span class="ov-counterparty-name ov-counterparty-name--empty">Unknown counterparty</span>';

  // Edit — turns the name into an editable field on demand (NOT always-on). It is
  // distinct from Confirm: Edit changes the name, Confirm confirms it.
  const editButton =
    '<button type="button" class="ov-counterparty-edit" data-ov-edit>Edit</button>';

  // Confirm affordance is preserved verbatim: an "Unconfirmed" badge + a "Confirm"
  // button when the counterparty still needs human confirmation; a quiet
  // "Confirmed" text once confirmed.
  const stateMarkup = confirmed
    ? '<span class="ov-counterparty-state ov-counterparty-state--confirmed">Confirmed</span>'
    : `
        <span class="ov-counterparty-badge" data-ov-unconfirmed>Unconfirmed</span>
        <button type="button" class="ov-counterparty-confirm" data-ov-confirm${name ? "" : " disabled"}>
          Confirm
        </button>`;

  // Inline edit field — HIDDEN by default (data-ov-edit-field[hidden]); the Edit
  // button reveals it. Saving commits the trimmed value through onEntityFill (the
  // SAME write path the removed always-on input used); Cancel restores the
  // read-only name without committing. Pre-seeded with the current name so the
  // user edits rather than retypes.
  const editor = `
      <div class="ov-counterparty-editor" data-ov-edit-field hidden>
        <input
          type="text"
          class="ov-counterparty-edit-input"
          data-ov-entity-input
          value="${escapeHtml(name)}"
          placeholder="Counterparty legal name"
          autocomplete="off"
          spellcheck="false">
        <div class="ov-counterparty-edit-actions">
          <button type="button" class="ov-counterparty-edit-save" data-ov-edit-save>Save</button>
          <button type="button" class="ov-counterparty-edit-cancel" data-ov-edit-cancel>Cancel</button>
        </div>
      </div>`;

  // SENDER — the inbound Gmail sender, shown directly under the counterparty row
  // as a labelled fact line (reusing the shared `ov-fact-label` family). Omitted
  // entirely when absent (manual uploads), so the line never renders empty.
  const senderLine = sender
    ? `
      <div class="ov-fact ov-counterparty-sender" data-ov-sender>
        <span class="ov-fact-label">Sender</span>
        <span class="ov-fact-value">${escapeHtml(sender)}</span>
      </div>`
    : "";

  // The block carries its own "COUNTERPARTY" field label, matching the
  // "SENDER" / "RECEIVED" labels below it. It reuses the shared `ov-fact-label`
  // small-caps gray label class so the field labels read as one family.
  return `
    <section class="ov-counterparty"${confirmed ? "" : ' data-ov-state="unconfirmed"'} aria-label="Counterparty">
      <span class="ov-fact-label ov-counterparty-label">Counterparty</span>
      <div class="ov-counterparty-head" data-ov-name-row>
        ${displayName}
        ${editButton}
        <span class="ov-counterparty-status">${stateMarkup}</span>
      </div>
      ${editor}
      ${senderLine}
    </section>`;
}

// --- matter-facts strip ------------------------------------------------------

// Accept the shell's camelCase received key (receivedDate) or a snake_case/short
// form (received_at). Governing law and term are intentionally NOT read here:
// they duplicated the "Governing Law" and "Term and Survival" clauses already in
// the roster, so they were removed from the facts strip to avoid confusion.
function normalizeFacts(facts) {
  const f = facts || {};
  const receivedDate = f.receivedDate != null ? f.receivedDate : f.received_at;
  return { receivedDate };
}

function renderFacts(facts) {
  const items = [["Received", formatReceivedDate(facts.receivedDate)]];
  return `
    <section class="ov-facts" aria-label="NDA facts">
      ${items.map(([label, value]) => renderFact(label, value)).join("")}
    </section>`;
}

// The received date arrives as a raw ISO timestamp
// ("2026-06-17T00:39:30.708994+00:00"). Render it date-only and human-legibly,
// e.g. "17 Jun 2026" (mirrors the app's short-month toLocaleDateString idiom used
// by RepositoryModel.formatMatterDate, adding the year). Missing / empty /
// unparseable values return "" so renderFact falls back to the "—" placeholder.
function formatReceivedDate(value) {
  if (value == null) return "";
  const raw = String(value).trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function renderFact(label, value) {
  const text = String(value == null ? "" : value).trim();
  const valueMarkup = text
    ? `<span class="ov-fact-value">${escapeHtml(text)}</span>`
    : '<span class="ov-fact-value ov-fact-value--empty">—</span>';
  return `
    <div class="ov-fact">
      <span class="ov-fact-label">${escapeHtml(label)}</span>
      ${valueMarkup}
    </div>`;
}

// --- events ------------------------------------------------------------------

function bindEvents(containerEl, onConfirm, onEntityFill) {
  if (onConfirm) {
    const confirmButton = containerEl.querySelector("[data-ov-confirm]");
    confirmButton?.addEventListener("click", () => {
      if (confirmButton.disabled) return;
      onConfirm();
    });
  }

  bindEdit(containerEl, onEntityFill);
}

// The Edit pattern: the name is read-only until the user clicks "Edit". Edit
// reveals the hidden inline field (data-ov-edit-field) and hides the read-only
// name row; Save commits the trimmed value through onEntityFill (the SAME write
// path the old always-on input used); Cancel restores the read-only name without
// committing. Editing is OFF by default — there is no always-on free-text input.
function bindEdit(containerEl, onEntityFill) {
  const editButton = containerEl.querySelector("[data-ov-edit]");
  const editField = containerEl.querySelector("[data-ov-edit-field]");
  const nameRow = containerEl.querySelector("[data-ov-name-row]");
  const input = containerEl.querySelector("[data-ov-entity-input]");
  // Without the editor markup there is nothing to gate; bail (e.g. an
  // unknown-counterparty render still gets here harmlessly).
  if (!editButton || !editField || !input) return;

  const setHidden = (el, hidden) => {
    if (!el) return;
    if (hidden) el.setAttribute("hidden", "");
    else el.removeAttribute("hidden");
  };

  const openEditor = () => {
    setHidden(editField, false);
    setHidden(nameRow, true);
    // Re-seed from the currently-displayed name each open and focus for typing.
    try {
      input.focus();
      if (typeof input.select === "function") input.select();
    } catch {
      /* focus/select are best-effort (absent in the test DOM). */
    }
  };

  const closeEditor = () => {
    setHidden(editField, true);
    setHidden(nameRow, false);
  };

  const save = () => {
    const value = String(input.value || "").trim();
    // Reuse the existing write path (submitCounterpartyOverride behind the shell's
    // onEntityFill); only commit when a handler is wired and the value is non-empty
    // so an empty Save never blanks the counterparty.
    if (typeof onEntityFill === "function" && value) onEntityFill(value);
    closeEditor();
  };

  editButton.addEventListener("click", openEditor);

  const saveButton = containerEl.querySelector("[data-ov-edit-save]");
  saveButton?.addEventListener("click", save);

  const cancelButton = containerEl.querySelector("[data-ov-edit-cancel]");
  cancelButton?.addEventListener("click", closeEditor);

  // Enter saves, Escape cancels — keyboard parity with the buttons.
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeEditor();
    }
  });
}

// --- helpers -----------------------------------------------------------------

// File-local HTML escaper. Named escapeHtml (not the bare `escape`) so a classic
// script declaration never clobbers the browser's built-in window.escape.
function escapeHtml(value) {
  // Delegate to the canonical bridged escaper ONLY when it is a *different*
  // function. This file is a classic <script>, so this top-level `function
  // escapeHtml` auto-binds to window.escapeHtml until global-bridge.mjs (a
  // deferred module) overwrites it with the html-utils escaper. Without the
  // `!== escapeHtml` guard the pre-bridge render path has window.escapeHtml
  // resolve to THIS function -> infinite recursion -> "Maximum call stack size
  // exceeded". The inline fallback below matches html-utils byte-for-byte.
  return typeof window !== "undefined"
    && typeof window.escapeHtml === "function"
    && window.escapeHtml !== escapeHtml
    ? window.escapeHtml(value)
    : String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Browser bridge so the shell can resolve the renderer off window, plus a
// CommonJS export so the Node frontend tests can require this script directly
// (the export guard is a no-op in the browser).
if (typeof window !== "undefined") {
  window.renderOverviewFacts = renderOverviewFacts;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderOverviewFacts };
}
