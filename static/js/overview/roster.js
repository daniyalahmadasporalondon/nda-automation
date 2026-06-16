// Overview tab — clause roster.
//
// A self-contained, presentational component for the new "Overview" tab. It
// paints a vertical roster of the matter's clauses, problems-first, so counsel
// sees what still needs attention before what's already clean. There is NO
// fetching, NO global state, and NO AI here — the shell (`overview-shell`) owns
// all data loading and shared-file wiring and simply hands us a clause list plus
// a click callback.
//
// Classic browser-script style (mirrors corpus.js / dashboard-search.js): the
// render entry point is exposed as a global function for the shell to call, with
// a CommonJS guard at the bottom so the Node FE test harness can import the pure
// helpers without a DOM.
//
// Public entry point:
//   renderOverviewRoster(containerEl, { clauses, currentClauseId }, { onClauseClick })
//
// Logical clause shape (field names are being finalized by the backend-data
// teammate; integrator reconciles): { id, name, verdict, reviewed }
//   * verdict: 'pass' | 'review' | 'fail'   (anything else is treated as 'review')
//   * reviewed: boolean                      (renders the ov-check when true)
//
// Shared class contract (a CSS teammate styles these — we only emit markup):
//   ov-roster                       wrapper on the container's content
//   ov-row                          one clause row
//   ov-row--current                 the row for currentClauseId
//   ov-pill ov-pill--pass|--review|--fail   the verdict pill
//   ov-check                        the "reviewed" check (only when reviewed)

const OverviewRoster = (() => {
  // The three verdicts we know how to paint, worst-first. The index doubles as
  // the sort key so problems float to the top of the roster.
  const VERDICT_ORDER = ["fail", "review", "pass"];

  // Human-facing labels for the verdict pill. Kept tiny and presentational.
  const VERDICT_LABEL = {
    fail: "Fail",
    review: "Needs review",
    pass: "Pass",
  };

  // Normalize whatever the data layer hands us into one of our three known
  // verdicts. Unknown / missing verdicts are the conservative "review" (never
  // silently "pass") so an undecided clause still surfaces above the clean ones.
  function normalizeVerdict(verdict) {
    const value = String(verdict || "").trim().toLowerCase();
    return VERDICT_ORDER.includes(value) ? value : "review";
  }

  // Escape untrusted text before it touches innerHTML. Clause names come from
  // user/AI data, so this is load-bearing, not cosmetic.
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  // Stable, problems-first ordering: fail, then review, then pass. Ties within a
  // verdict keep the caller's incoming order (stable sort) so the data layer
  // stays in control of intra-bucket ordering. Returns a new array — never
  // mutates the caller's list.
  function sortClauses(clauses) {
    const list = Array.isArray(clauses) ? clauses.slice() : [];
    return list
      .map((clause, index) => ({ clause, index }))
      .sort((a, b) => {
        const rankA = VERDICT_ORDER.indexOf(normalizeVerdict(a.clause && a.clause.verdict));
        const rankB = VERDICT_ORDER.indexOf(normalizeVerdict(b.clause && b.clause.verdict));
        if (rankA !== rankB) return rankA - rankB;
        return a.index - b.index; // stable within a verdict bucket
      })
      .map((entry) => entry.clause);
  }

  // Build the markup for a single row. `current` flags the active clause so it
  // gets the ov-row--current modifier. We stash the clause id on a data
  // attribute so the click delegation can recover it without a closure per row.
  function rowHtml(clause, { current } = {}) {
    const verdict = normalizeVerdict(clause && clause.verdict);
    const id = clause && clause.id != null ? String(clause.id) : "";
    const name = clause && clause.name != null ? clause.name : "";
    const reviewed = Boolean(clause && clause.reviewed);

    const rowClass = current ? "ov-row ov-row--current" : "ov-row";
    const checkHtml = reviewed
      ? '<span class="ov-check" aria-label="Reviewed" title="Reviewed">✓</span>'
      : "";

    return (
      `<div class="${rowClass}" role="button" tabindex="0" data-clause-id="${escapeHtml(id)}">` +
      `<span class="ov-row__name">${escapeHtml(name)}</span>` +
      `<span class="ov-pill ov-pill--${verdict}">${escapeHtml(VERDICT_LABEL[verdict])}</span>` +
      checkHtml +
      `</div>`
    );
  }

  // Render the whole roster into `containerEl`. Pure DOM glue: sort, build the
  // rows, set innerHTML once, then wire a single delegated click/keyboard
  // handler that resolves the clicked row back to its clause id and calls the
  // injected onClauseClick seam.
  function render(containerEl, data, handlers) {
    if (!containerEl) return;
    const { clauses, currentClauseId } = data && typeof data === "object" ? data : {};
    const onClauseClick =
      handlers && typeof handlers.onClauseClick === "function" ? handlers.onClauseClick : null;

    const ordered = sortClauses(clauses);
    const currentId = currentClauseId == null ? null : String(currentClauseId);

    const rows = ordered
      .map((clause) => {
        const id = clause && clause.id != null ? String(clause.id) : "";
        const current = currentId != null && id === currentId;
        return rowHtml(clause, { current });
      })
      .join("");

    containerEl.innerHTML = `<div class="ov-roster">${rows}</div>`;

    if (!onClauseClick) return;

    const fire = (target) => {
      const row = target && target.closest ? target.closest(".ov-row") : null;
      if (!row || !containerEl.contains(row)) return;
      const id = row.getAttribute("data-clause-id");
      onClauseClick(id);
    };

    containerEl.addEventListener("click", (event) => fire(event.target));
    containerEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fire(event.target);
      }
    });
  }

  return { render, sortClauses, normalizeVerdict, rowHtml, VERDICT_ORDER, VERDICT_LABEL };
})();

// Public entry point the overview-shell calls to paint the roster.
function renderOverviewRoster(containerEl, data, handlers) {
  return OverviewRoster.render(containerEl, data || {}, handlers || {});
}

// Node test-harness export (no-op in the browser, exactly like corpus.js). Lets
// the FE unit test import the pure helpers + the entry point without a DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { OverviewRoster, renderOverviewRoster };
}
