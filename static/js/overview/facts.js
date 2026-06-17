// Overview tab — counterparty + matter-facts surface.
//
// One self-contained renderer for the new "Overview" inspector tab. It paints two
// things into a caller-supplied container:
//
//   1. A COUNTERPARTY block (`ov-counterparty`): the extracted counterparty name
//      with its confirmed / unconfirmed state. When the counterparty still needs
//      human confirmation it shows an "Unconfirmed" badge and a "Confirm" control
//      (-> onConfirm()); this mirrors the existing review-workstation counterparty
//      field (matter.counterparty_needs_confirmation !== false drives the badge).
//      The block also carries the entity-name fill input that absorbs the old
//      "Fill" tab's entity-name function — typing a name and committing it fires
//      onEntityFill(value).
//
//   2. A matter-FACTS strip (`ov-facts`) of `ov-fact` items for governing law,
//      term, and received date.
//
// The shell owns the shared files (index.html / styles.css) and wires the two
// callbacks to the app's existing confirm + entity-fill logic, so this module is
// pure render + event-dispatch: it never fetches, never mutates app state, and
// never reaches outside `containerEl`. A CSS teammate styles the shared classes
// (`ov-counterparty`, `ov-facts`, `ov-fact`); we only emit them.
//
// escapeHtml is resolved lazily via window.escapeHtml (same pattern as the Fill
// tab) so the module loads with or without the page's helper present.

function renderOverviewFacts(containerEl, data, handlers) {
  if (!containerEl) return;

  const model = data || {};
  const counterparty = model.counterparty || {};
  const facts = model.facts || {};
  const callbacks = handlers || {};
  const onConfirm = typeof callbacks.onConfirm === "function" ? callbacks.onConfirm : null;
  const onEntityFill = typeof callbacks.onEntityFill === "function" ? callbacks.onEntityFill : null;

  const name = String(counterparty.name == null ? "" : counterparty.name).trim();
  // Trust the caller's confirmed flag; absent / non-true reads as unconfirmed so
  // the Confirm control fails open the same way the backend badge decision does.
  const confirmed = counterparty.confirmed === true;

  containerEl.innerHTML = `
    ${renderCounterparty(name, confirmed)}
    ${renderFacts(facts)}
  `;

  bindEvents(containerEl, onConfirm, onEntityFill);
}

// --- counterparty block ------------------------------------------------------

function renderCounterparty(name, confirmed) {
  const displayName = name
    ? `<span class="ov-counterparty-name" title="${escape(name)}">${escape(name)}</span>`
    : '<span class="ov-counterparty-name ov-counterparty-name--empty">Unknown counterparty</span>';

  const stateMarkup = confirmed
    ? '<span class="ov-counterparty-state ov-counterparty-state--confirmed">Confirmed</span>'
    : `
        <span class="ov-counterparty-badge" data-ov-unconfirmed>Unconfirmed</span>
        <button type="button" class="ov-counterparty-confirm" data-ov-confirm${name ? "" : " disabled"}>
          Confirm
        </button>`;

  // The entity-name fill input absorbs the old "Fill" tab's entity-name function:
  // committing a value (change/Enter) hands the trimmed name to onEntityFill.
  return `
    <section class="ov-counterparty"${confirmed ? "" : ' data-ov-state="unconfirmed"'} aria-label="Counterparty">
      <div class="ov-counterparty-head">
        ${displayName}
        <span class="ov-counterparty-status">${stateMarkup}</span>
      </div>
      <label class="ov-counterparty-fill">
        <span class="ov-counterparty-fill-label">Entity name</span>
        <input
          type="text"
          class="ov-counterparty-fill-input"
          data-ov-entity-fill
          value="${escape(name)}"
          placeholder="Counterparty legal name"
          autocomplete="off"
          spellcheck="false">
      </label>
    </section>`;
}

// --- matter-facts strip ------------------------------------------------------

function renderFacts(facts) {
  const items = [
    ["Governing law", facts.governingLaw],
    ["Term", facts.term],
    ["Received", facts.receivedDate],
  ];
  return `
    <section class="ov-facts" aria-label="Matter facts">
      ${items.map(([label, value]) => renderFact(label, value)).join("")}
    </section>`;
}

function renderFact(label, value) {
  const text = String(value == null ? "" : value).trim();
  const valueMarkup = text
    ? `<span class="ov-fact-value">${escape(text)}</span>`
    : '<span class="ov-fact-value ov-fact-value--empty">—</span>';
  return `
    <div class="ov-fact">
      <span class="ov-fact-label">${escape(label)}</span>
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

  if (onEntityFill) {
    const fillInput = containerEl.querySelector("[data-ov-entity-fill]");
    if (fillInput) {
      // Commit on change (blur / explicit value change), matching how the Fill tab
      // hands a chosen entity onward. Enter commits without waiting for blur.
      const commit = () => onEntityFill(String(fillInput.value || "").trim());
      fillInput.addEventListener("change", commit);
      fillInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commit();
        }
      });
    }
  }
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

// Browser bridge so the shell can resolve the renderer off window, plus a
// CommonJS export so the Node frontend tests can require this script directly
// (the export guard is a no-op in the browser).
if (typeof window !== "undefined") {
  window.renderOverviewFacts = renderOverviewFacts;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderOverviewFacts };
}
