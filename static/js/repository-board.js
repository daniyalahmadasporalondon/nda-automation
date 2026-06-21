const RepositoryBoard = (() => {
  function html(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") {
      return window.escapeHtml(value);
    }
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function renderBoard({
    errorMessage = "",
    gmailDemoMatterList,
    handlers,
    pendingDeleteMatterId,
    searchQuery = "",
    selectedMatter,
    state,
  }) {
    if (!gmailDemoMatterList) return;
    const mattersByColumn = new Map(RepositoryModel.BOARD_COLUMNS.map((column) => [column.id, []]));
    const query = normalizeSearchText(searchQuery);
    // The board is WIP only: an EXECUTED (fully-signed) matter is done and drops
    // off the board, so it is never bucketed into a column. The backend already
    // excludes it from the payload; this is the frontend backstop.
    state.matters
      .filter((matter) => !RepositoryModel.isMatterExecuted(matter))
      .filter((matter) => matterMatchesSearch(matter, query))
      .forEach((matter) => {
        mattersByColumn.get(RepositoryModel.matterColumn(matter)).push(matter);
      });
    mattersByColumn.forEach((matters) => matters.sort(RepositoryModel.compareMatterRecency));
    document.querySelectorAll("[data-repository-count]").forEach((count) => {
      count.textContent = String(mattersByColumn.get(count.dataset.repositoryCount)?.length || 0);
    });
    renderSyncStatus(state);
    // A fresh user with no matters at all sees six "No documents" columns, which
    // reads as broken rather than empty. Surface a friendly onboarding panel that
    // tells them how to get their first NDA on the board. It only shows for a
    // genuinely empty board with no active error and no active search (a search
    // that finds nothing is a different, already-handled state).
    renderBoardOnboarding({ state, errorMessage, searchActive: Boolean(query) });
    document.querySelectorAll("[data-repository-list]").forEach((list) => {
      const matters = mattersByColumn.get(list.dataset.repositoryList) || [];
      list.innerHTML = errorMessage
        ? `<div class="repository-dropzone">${html(errorMessage)}</div>`
        : matters.length
        ? matters.map((matter) => renderMatterCard(matter, { confirmingDelete: matter.id === pendingDeleteMatterId })).join("")
        : `<div class="repository-dropzone">${query ? "No matching documents" : "No documents"}</div>`;
      bindBoardEvents(list, { handlers, selectedMatter });
    });
  }

  function matterMatchesSearch(matter, query) {
    if (!query) return true;
    return searchableMatterText(matter).includes(query);
  }

  function searchableMatterText(matter) {
    return normalizeSearchText([
      RepositoryModel.matterSubject(matter),
      RepositoryModel.matterSender(matter),
      matter?.message_snippet,
      matter?.attachment_filename,
      matter?.source_filename,
      matter?.document_title,
      matter?.received_at,
      RepositoryModel.sourceTypeLabel(matter?.source_type),
      RepositoryModel.matterColumnLabel(matter),
      MatterUtils.reviewStale(matter) ? "stale" : "",
    ].filter(Boolean).join(" "));
  }

  function normalizeSearchText(value) {
    return String(value || "").trim().toLowerCase();
  }

  function bindBoardEvents(list, { handlers, selectedMatter }) {
    list.querySelectorAll("[data-matter-id]").forEach((card) => {
      card.classList.toggle("active", card.dataset.matterId === selectedMatter?.id);
      card.addEventListener("click", () => handlers.openMatter(card.dataset.matterId));
      card.addEventListener("keydown", (event) => {
        if (event.target !== card || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        handlers.openMatter(card.dataset.matterId);
      });
    });
    list.querySelectorAll("[data-delete-matter-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        handlers.requestDeleteMatter(button.dataset.deleteMatterId);
      });
    });
    list.querySelectorAll("[data-delete-confirmation-id]").forEach((confirmation) => {
      confirmation.addEventListener("click", (event) => {
        event.stopPropagation();
      });
    });
    list.querySelectorAll("[data-cancel-delete-matter-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        handlers.cancelDeleteMatter(button.dataset.cancelDeleteMatterId);
      });
    });
    list.querySelectorAll("[data-confirm-delete-matter-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        handlers.deleteMatter(button.dataset.confirmDeleteMatterId, button);
      });
    });
  }

  function renderSyncStatus(state) {
    const node = document.querySelector("[data-repository-sync-status]");
    if (!node) return;
    const sync = state.gmailStatus?.sync || state.gmailStatus?.settings || {};
    const inbound = state.gmailStatus?.inbound || {};
    const recentRun = Array.isArray(sync.sync_history) ? sync.sync_history[0] : null;
    const inboundSetupBlocked = inbound.enabled !== false && inbound.ready === false;
    node.classList.toggle("error", inboundSetupBlocked || recentRun?.status === "error");
    if (inbound.enabled === false) {
      node.textContent = "Gmail inbound paused";
      return;
    }
    if (inboundSetupBlocked) {
      node.textContent = `Gmail inbound setup required: ${inbound.error || "check Admin"}`;
      return;
    }
    if (recentRun?.status === "error") {
      node.textContent = `Last sync error: ${recentRun.error || "check Admin"}`;
      return;
    }
    if (!sync.last_sync_at) {
      node.textContent = state.gmailStatus?.user_scoped ? "Waiting for your Gmail sync" : "Waiting for scheduled sync";
      return;
    }
    const imported = Number(sync.last_sync_imported_count || 0);
    const skipped = Number(sync.last_sync_skipped_count || 0);
    const ownerLabel = state.gmailStatus?.user_scoped ? "Your last sync" : "Last sync";
    node.textContent = `${ownerLabel} ${RepositoryModel.formatMatterDateTime(sync.last_sync_at) || sync.last_sync_at} - ${imported} imported / ${skipped} skipped`;
  }

  // Whether the user's Gmail inbound is actually wired up. We only suppress the
  // "Connect Gmail" onboarding nudge when inbound is genuinely ready; any unknown
  // / not-ready / paused state keeps the nudge (fail toward showing guidance).
  function gmailInboundReady(state) {
    const inbound = state?.gmailStatus?.inbound;
    return Boolean(inbound && inbound.ready === true);
  }

  function renderBoardOnboarding({ state, errorMessage = "", searchActive = false } = {}) {
    const node = document.querySelector("[data-repository-onboarding]");
    if (!node) return;
    const totalMatters = Array.isArray(state?.matters) ? state.matters.length : 0;
    // Show ONLY for a truly fresh board: no matters, no error to surface, and no
    // active search (the per-column "No matching documents" covers that case).
    const show = totalMatters === 0 && !errorMessage && !searchActive;
    node.hidden = !show;
    if (!show) {
      node.innerHTML = "";
      return;
    }
    const gmailReady = gmailInboundReady(state);
    const gmailStep = gmailReady
      ? `<li class="repository-onboarding-step is-done">
           <span class="repository-onboarding-step-icon" aria-hidden="true">✓</span>
           <span class="repository-onboarding-step-body">
             <strong>Gmail is connected</strong>
             <span>Inbound NDAs will appear in the Inbox column automatically.</span>
           </span>
         </li>`
      : `<li class="repository-onboarding-step">
           <span class="repository-onboarding-step-icon" aria-hidden="true">2</span>
           <span class="repository-onboarding-step-body">
             <strong>Connect Gmail to import inbound NDAs</strong>
             <span>Incoming NDAs land in your Inbox column, ready to review.</span>
             <button class="repository-onboarding-action secondary" type="button" data-onboarding-goto="admin">Connect Gmail</button>
           </span>
         </li>`;
    node.innerHTML = `
      <div class="repository-onboarding-card" role="note" aria-label="Get started with the contract repository">
        <h2 class="repository-onboarding-title">Welcome — let's get your first NDA here</h2>
        <p class="repository-onboarding-lead">Your repository is empty. NDAs show up here once you generate one or connect an inbox.</p>
        <ol class="repository-onboarding-steps">
          <li class="repository-onboarding-step">
            <span class="repository-onboarding-step-icon" aria-hidden="true">1</span>
            <span class="repository-onboarding-step-body">
              <strong>Generate your first NDA</strong>
              <span>Draft a fresh NDA from your playbook in a couple of clicks.</span>
              <button class="repository-onboarding-action" type="button" data-onboarding-goto="generator">Open the Generator</button>
            </span>
          </li>
          ${gmailStep}
        </ol>
      </div>
    `;
  }

  function renderMatterCard(matter, options = {}) {
    const issueCount = Number(matter.issue_count || 0);
    // The issue count is a DISPLAY of a verdict, so it only shows once an AI review
    // has actually run (ai_review_ran). A deterministic-only matter shows "Pending"
    // instead -- issue_count's triage/routing/search uses are untouched. Fall back
    // to "show the count" only for fixtures predating the flag.
    const aiReviewRan = typeof matter.ai_review_ran === "boolean" ? matter.ai_review_ran : true;
    // The issue count foot only carries meaning once an AI review has run; an
    // un-reviewed card's status is carried by the review badge instead (so we
    // don't show both a "Pending" foot AND a "Not reviewed" badge -- redundant).
    const issueLabel = aiReviewRan
      ? `${issueCount} ${issueCount === 1 ? "issue" : "issues"}`
      : "";
    // Universal review-status badge across every column. While a background AI
    // review is running (review_status === "in_progress") the card shows a live
    // "Reviewing…" badge that supersedes the reviewed/not-reviewed state; otherwise
    // it is quiet/green when the AI has reviewed and amber when it has not -- text
    // label (not colour-only) for a11y.
    const reviewInProgress = MatterUtils.reviewInProgress(matter);
    const reviewBadge = reviewInProgress
      ? `<span class="repository-review-badge reviewing" aria-busy="true" title="An AI review is running on this NDA in the background.">Reviewing…</span>`
      : aiReviewRan
        ? `<span class="repository-review-badge reviewed" title="An AI review has been run on this NDA.">AI reviewed</span>`
        : `<span class="repository-review-badge pending" title="No AI review has run on this NDA yet. Open the NDA to review it.">Not reviewed</span>`;
    const date = RepositoryModel.formatMatterDate(matter.received_at || matter.created_at);
    const confirmingDelete = Boolean(options.confirmingDelete);
    return `
      <article class="repository-card ${confirmingDelete ? "deleting" : ""}" role="button" tabindex="0" data-matter-id="${html(matter.id)}" aria-label="Open NDA ${html(RepositoryModel.matterSubject(matter))}">
        <span class="repository-card-top">
          <span class="repository-card-badges">
            <span class="repository-source-badge ${html(RepositoryModel.sourceBadgeClass(matter.source_type))}">${html(RepositoryModel.sourceTypeLabel(matter.source_type))}</span>
            ${reviewBadge}
            ${!reviewInProgress && MatterUtils.reviewStale(matter) ? `<span class="repository-stale-badge" title="${html(MatterUtils.reviewStaleLabel(matter))}">Stale</span>` : ""}
          </span>
          <span class="repository-card-top-actions">
            <span>${html(date)}</span>
            <button class="repository-card-delete" type="button" data-delete-matter-id="${html(matter.id)}" aria-label="Delete NDA" title="Delete NDA" aria-expanded="${confirmingDelete ? "true" : "false"}">x</button>
          </span>
        </span>
        <strong>${html(RepositoryModel.matterSubject(matter))}</strong>
        <span class="repository-card-source">${html(RepositoryModel.matterSender(matter))}</span>
        <span class="repository-card-snippet">${html(matter.message_snippet || matter.attachment_filename || matter.source_filename || RepositoryModel.sourceTypeLabel(matter.source_type))}</span>
        ${confirmingDelete ? renderMatterDeleteConfirmation(matter) : ""}
        <span class="repository-card-rule"></span>
        <span class="repository-card-foot">
          <span>${html(issueLabel)}</span>
          <span>${html(RepositoryModel.matterColumnLabel(matter))}</span>
        </span>
      </article>
    `;
  }

  function renderMatterDeleteConfirmation(matter) {
    const matterId = html(matter.id);
    return `
      <div class="repository-delete-confirmation" role="group" aria-label="Delete NDA confirmation" data-delete-confirmation-id="${matterId}">
        <span>Delete NDA and stored document?</span>
        <span class="repository-delete-confirmation-actions">
          <button class="secondary repository-delete-cancel" type="button" data-cancel-delete-matter-id="${matterId}" aria-label="Cancel delete NDA">Cancel</button>
          <button class="repository-delete-confirm-button" type="button" data-confirm-delete-matter-id="${matterId}" aria-label="Confirm delete NDA">Delete</button>
        </span>
      </div>
    `;
  }

  return { renderBoard, renderBoardOnboarding, renderMatterCard, renderSyncStatus };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { RepositoryBoard };
}
