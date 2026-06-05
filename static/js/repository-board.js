const RepositoryBoard = (() => {
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
    state.matters.filter((matter) => matterMatchesSearch(matter, query)).forEach((matter) => {
      mattersByColumn.get(RepositoryModel.matterColumn(matter)).push(matter);
    });
    mattersByColumn.forEach((matters) => matters.sort(RepositoryModel.compareMatterRecency));
    document.querySelectorAll("[data-repository-count]").forEach((count) => {
      count.textContent = String(mattersByColumn.get(count.dataset.repositoryCount)?.length || 0);
    });
    renderSyncStatus(state);
    document.querySelectorAll("[data-repository-list]").forEach((list) => {
      const matters = mattersByColumn.get(list.dataset.repositoryList) || [];
      list.innerHTML = errorMessage
        ? `<div class="repository-dropzone">${escapeHtml(errorMessage)}</div>`
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
      RepositoryModel.boardColumnLabel(matter?.board_column),
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

  function renderMatterCard(matter, options = {}) {
    const issueCount = Number(matter.issue_count || 0);
    const date = RepositoryModel.formatMatterDate(matter.received_at || matter.created_at);
    const confirmingDelete = Boolean(options.confirmingDelete);
    return `
      <article class="repository-card ${confirmingDelete ? "deleting" : ""}" role="button" tabindex="0" data-matter-id="${escapeHtml(matter.id)}" aria-label="Open matter ${escapeHtml(RepositoryModel.matterSubject(matter))}">
        <span class="repository-card-top">
          <span class="repository-card-badges">
            <span class="repository-source-badge ${escapeHtml(RepositoryModel.sourceBadgeClass(matter.source_type))}">${escapeHtml(RepositoryModel.sourceTypeLabel(matter.source_type))}</span>
          </span>
          <span class="repository-card-top-actions">
            <span>${escapeHtml(date)}</span>
            <button class="repository-card-delete" type="button" data-delete-matter-id="${escapeHtml(matter.id)}" aria-label="Delete matter" title="Delete matter" aria-expanded="${confirmingDelete ? "true" : "false"}">x</button>
          </span>
        </span>
        <strong>${escapeHtml(RepositoryModel.matterSubject(matter))}</strong>
        <span class="repository-card-source">${escapeHtml(RepositoryModel.matterSender(matter))}</span>
        <span class="repository-card-snippet">${escapeHtml(matter.message_snippet || matter.attachment_filename || matter.source_filename || RepositoryModel.sourceTypeLabel(matter.source_type))}</span>
        ${confirmingDelete ? renderMatterDeleteConfirmation(matter) : ""}
        <span class="repository-card-rule"></span>
        <span class="repository-card-foot">
          <span>${issueCount} ${issueCount === 1 ? "issue" : "issues"}</span>
          <span>${escapeHtml(RepositoryModel.boardColumnLabel(matter.board_column))}</span>
        </span>
      </article>
    `;
  }

  function renderMatterDeleteConfirmation(matter) {
    const matterId = escapeHtml(matter.id);
    return `
      <div class="repository-delete-confirmation" role="group" aria-label="Delete matter confirmation" data-delete-confirmation-id="${matterId}">
        <span>Delete matter and stored document?</span>
        <span class="repository-delete-confirmation-actions">
          <button class="secondary repository-delete-cancel" type="button" data-cancel-delete-matter-id="${matterId}" aria-label="Cancel delete matter">Cancel</button>
          <button class="repository-delete-confirm-button" type="button" data-confirm-delete-matter-id="${matterId}" aria-label="Confirm delete matter">Delete</button>
        </span>
      </div>
    `;
  }

  return { renderBoard, renderMatterCard, renderSyncStatus };
})();
