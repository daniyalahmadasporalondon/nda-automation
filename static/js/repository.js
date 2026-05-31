const RepositoryView = (() => {
  const BOARD_COLUMNS = [
    { id: "gmail_demo", label: "Gmail Demo" },
    { id: "in_review", label: "In Review" },
    { id: "redline_ready", label: "Redline Ready" },
    { id: "signed_closed", label: "Signed / Closed" },
  ];

  function createController({
    state,
    gmailDemoStatus,
    gmailLastSync,
    gmailSyncButton,
    repositoryFileInput,
    repositoryDemoResetButton,
    gmailDemoMatterList,
    repositoryMatterPanel,
    repositoryImportStatus,
    downloadBlob,
    downloadFilename,
    fileToBase64,
    loadMatterIntoReview,
    redlineDownloadFilename,
    reviewErrorFromPayload,
  }) {
    let selectedMatter = null;
    let pendingSendMatterId = null;
    const repositoryWorkspace = repositoryMatterPanel?.closest(".repository-workspace");
    const boardColumnIds = new Set(BOARD_COLUMNS.map((column) => column.id));

    gmailSyncButton?.addEventListener("click", syncGmail);
    repositoryDemoResetButton?.addEventListener("click", resetDemoRepository);

    repositoryFileInput?.addEventListener("change", async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      await importMatter(file);
      repositoryFileInput.value = "";
    });

    async function importMatter(file) {
      if (!isReviewableDocument(file.name)) {
        setImportStatus("Upload a .docx Word document or text-based PDF");
        return;
      }

      setImportStatus(`Importing ${file.name}`);
      try {
        const response = await fetch("/api/matters", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: file.name,
            content_base64: await fileToBase64(file),
            source_type: "gmail_demo",
            sender: "Manual upload",
            subject: documentTitleFromFilename(file.name),
            received_at: new Date().toISOString(),
            message_snippet: `Manual upload of ${file.name}.`,
            attachment_filename: file.name,
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Import could not run");
        await loadMatters();
        if (payload.matter?.id) {
          await openMatter(payload.matter.id);
        }
        setImportStatus(`${payload.matter.document_title || file.name} imported`);
      } catch (error) {
        setImportStatus(error.message || "Import could not run");
      }
    }

    async function syncGmail() {
      if (!gmailSyncButton) return;
      const originalText = gmailSyncButton.textContent;
      gmailSyncButton.disabled = true;
      gmailSyncButton.textContent = "Syncing";
      setImportStatus("Checking Gmail");
      try {
        const response = await fetch("/api/gmail/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ limit: 10 }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail sync could not run");
        const imported = Array.isArray(payload.imported) ? payload.imported : [];
        const skipped = Array.isArray(payload.skipped) ? payload.skipped : [];
        await loadMatters();
        if (imported[0]?.id) {
          await openMatter(imported[0].id);
        }
        updateLastSync(payload.account || "");
        setImportStatus(gmailSyncSummary(imported, skipped));
      } catch (error) {
        setImportStatus(error.message || "Gmail sync could not run");
      } finally {
        gmailSyncButton.disabled = false;
        gmailSyncButton.textContent = originalText || "Sync Gmail";
      }
    }

    async function resetDemoRepository() {
      if (!repositoryDemoResetButton) return;
      const originalText = repositoryDemoResetButton.textContent;
      repositoryDemoResetButton.disabled = true;
      repositoryDemoResetButton.textContent = "Resetting";
      setImportStatus("Resetting demo repository");
      try {
        const response = await fetch("/api/demo/reset", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Demo reset could not run");
        state.matters = [];
        selectedMatter = null;
        pendingSendMatterId = null;
        renderEmptyPanel();
        renderBoard();
        setImportStatus(`Demo reset. Removed ${Number(payload.removed || 0)} matters.`);
      } catch (error) {
        setImportStatus(error.message || "Demo reset could not run");
      } finally {
        repositoryDemoResetButton.disabled = false;
        repositoryDemoResetButton.textContent = originalText || "Reset Demo";
      }
    }

    async function loadGmailStatus() {
      if (!gmailDemoStatus) return;
      try {
        const response = await fetch("/api/gmail/status");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail status could not load");
        renderGmailStatus(payload.gmail || {});
      } catch (error) {
        renderGmailStatus({
          inbound: { ready: false, error: error.message || "Status unavailable" },
          outbound: { ready: false, error: error.message || "Status unavailable" },
        });
      }
    }

    async function loadMatters() {
      if (!gmailDemoMatterList) return;
      try {
        const response = await fetch("/api/matters");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Repository could not load");
        state.matters = Array.isArray(payload.matters) ? payload.matters : [];
        if (selectedMatter && !state.matters.find((matter) => matter.id === selectedMatter.id)) {
          selectedMatter = null;
          renderEmptyPanel();
        }
        renderBoard();
      } catch (error) {
        gmailDemoMatterList.innerHTML = `<div class="repository-dropzone">${escapeHtml(error.message)}</div>`;
      }
    }

    function renderBoard() {
      const mattersByColumn = new Map(BOARD_COLUMNS.map((column) => [column.id, []]));
      state.matters.forEach((matter) => {
        const column = boardColumnIds.has(matter.board_column) ? matter.board_column : "gmail_demo";
        mattersByColumn.get(column).push(matter);
      });
      document.querySelectorAll("[data-repository-count]").forEach((count) => {
        count.textContent = String(mattersByColumn.get(count.dataset.repositoryCount)?.length || 0);
      });
      document.querySelectorAll("[data-repository-list]").forEach((list) => {
        const matters = mattersByColumn.get(list.dataset.repositoryList) || [];
        list.innerHTML = matters.length
          ? matters.map(renderMatterCard).join("")
          : '<div class="repository-dropzone">No documents</div>';
        list.querySelectorAll("[data-matter-id]").forEach((card) => {
          card.classList.toggle("active", card.dataset.matterId === selectedMatter?.id);
          card.addEventListener("click", () => openMatter(card.dataset.matterId));
        });
      });
    }

    async function openMatter(matterId) {
      try {
        const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}`);
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Matter could not load");
        selectedMatter = payload.matter;
        renderBoard();
        renderDetailPanel(payload.matter);
      } catch (error) {
        setImportStatus(error.message || "Matter could not load");
      }
    }

    function renderDetailPanel(matter) {
      if (!repositoryMatterPanel) return;
      const reviewResult = matter.review_result || {};
      const failedClauses = Array.isArray(reviewResult.clauses)
        ? reviewResult.clauses.filter((clause) => clause && clause.passes === false)
        : [];
      const isClosed = matter.board_column === "signed_closed";
      const subject = matterSubject(matter);
      const recipient = MatterUtils.recipientEmail(matter);
      const canSendRedline = MatterUtils.canSendRedline(matter);
      const confirmingSend = pendingSendMatterId === matter.id;
      repositoryMatterPanel.hidden = false;
      repositoryWorkspace?.classList.add("detail-open");
      repositoryMatterPanel.innerHTML = `
        <header class="repository-detail-head">
          <div>
            <p class="repository-detail-kicker">${escapeHtml(sourceTypeLabel(matter.source_type))}</p>
            <h2>${escapeHtml(subject)}</h2>
          </div>
          <button class="repository-detail-close" type="button" aria-label="Close matter panel">x</button>
        </header>
        <div class="repository-detail-status">
          <span class="repository-priority">${escapeHtml(triageLabel(matter.triage_status))}</span>
          <strong>${escapeHtml(boardColumnLabel(matter.board_column))}</strong>
          <span>${Number(matter.issue_count || 0)} ${Number(matter.issue_count || 0) === 1 ? "issue" : "issues"}</span>
        </div>
        <section class="repository-detail-email">
          <dl>
            <div>
              <dt>From</dt>
              <dd>${escapeHtml(matterSender(matter))}</dd>
            </div>
            <div>
              <dt>Received</dt>
              <dd>${escapeHtml(formatMatterDateTime(matter.received_at || matter.created_at) || "-")}</dd>
            </div>
            <div>
              <dt>Attachment</dt>
              <dd>${escapeHtml(matter.attachment_filename || matter.source_filename || "-")}</dd>
            </div>
          </dl>
          <p>${escapeHtml(matter.message_snippet || "No message preview available.")}</p>
        </section>
        <dl class="repository-detail-meta">
          <div>
            <dt>Next action</dt>
            <dd>${escapeHtml(matter.next_action || "Review")}</dd>
          </div>
          <div>
            <dt>Requirements</dt>
            <dd>${Number(matter.requirements_passed || 0)} passed / ${Number(matter.requirements_failed || 0)} failed</dd>
          </div>
          ${matter.last_outbound_at ? `
            <div>
              <dt>Last sent</dt>
              <dd>${escapeHtml(formatMatterDateTime(matter.last_outbound_at))}</dd>
            </div>
          ` : ""}
        </dl>
        <section class="repository-detail-issues">
          <h3>Key failed clauses</h3>
          ${renderFailedClauses(failedClauses)}
        </section>
        <div class="repository-detail-actions">
          <button type="button" class="repository-open-review">Open Review</button>
          <button type="button" class="secondary repository-export-redline">Export Redline</button>
          <button type="button" class="secondary repository-send-redline ${confirmingSend ? "confirming" : ""}" ${canSendRedline ? "" : "disabled"}>${confirmingSend ? "Confirm Send" : "Send Redline"}</button>
          <button type="button" class="secondary repository-close-matter" ${isClosed ? "disabled" : ""}>Close Matter</button>
        </div>
        <p class="repository-detail-message" aria-live="polite"></p>
      `;
      repositoryMatterPanel.querySelector(".repository-detail-close")?.addEventListener("click", closePanel);
      repositoryMatterPanel.querySelector(".repository-open-review")?.addEventListener("click", () => openMatterInReview(matter));
      repositoryMatterPanel.querySelector(".repository-export-redline")?.addEventListener("click", () => exportMatter(matter));
      repositoryMatterPanel.querySelector(".repository-send-redline")?.addEventListener("click", () => sendRedline(matter));
      repositoryMatterPanel.querySelector(".repository-close-matter")?.addEventListener("click", () => closeMatterWorkflow(matter));
    }

    function renderEmptyPanel() {
      if (!repositoryMatterPanel) return;
      repositoryWorkspace?.classList.remove("detail-open");
      repositoryMatterPanel.hidden = true;
      repositoryMatterPanel.innerHTML = '<div class="repository-detail-empty">Select a matter</div>';
    }

    function closePanel() {
      selectedMatter = null;
      pendingSendMatterId = null;
      renderEmptyPanel();
      renderBoard();
    }

    async function openMatterInReview(matter) {
      pendingSendMatterId = null;
      const updatedMatter = await moveMatterToColumn(matter.id, "in_review", { quiet: true });
      selectedMatter = updatedMatter || matter;
      renderBoard();
      renderDetailPanel(selectedMatter);
      loadMatterIntoReview(selectedMatter);
    }

    async function exportMatter(matter) {
      pendingSendMatterId = null;
      const exportButton = repositoryMatterPanel?.querySelector(".repository-export-redline");
      setPanelMessage("");
      if (exportButton) {
        exportButton.disabled = true;
        exportButton.textContent = "Exporting";
      }
      try {
        const response = await fetch("/api/export-review-docx", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ matter_id: matter.id }),
        });
        if (!response.ok) {
          const payload = await response.json();
          throw reviewErrorFromPayload(payload, "Export could not run");
        }
        const filename = downloadFilename(response) || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx");
        const blob = await response.blob();
        downloadBlob(blob, filename);
        const movedMatter = await moveMatterToColumn(matter.id, "redline_ready", { quiet: true });
        setPanelMessage(movedMatter ? `Downloading ${filename}. Moved to Redline Ready.` : `Downloading ${filename}. Stage could not update.`);
      } catch (error) {
        setPanelMessage(error.message || "Export could not run");
      } finally {
        if (exportButton?.isConnected) {
          exportButton.disabled = false;
          exportButton.textContent = "Export Redline";
        }
      }
    }

    async function sendRedline(matter) {
      const recipient = MatterUtils.recipientEmail(matter);
      if (!recipient) {
        pendingSendMatterId = null;
        setPanelMessage("Matter sender is not an email address.");
        return;
      }
      if (pendingSendMatterId !== matter.id) {
        pendingSendMatterId = matter.id;
        renderDetailPanel(matter);
        setPanelMessage(`Click Confirm Send to email the redline to ${recipient}.`);
        return;
      }

      const sendButton = repositoryMatterPanel?.querySelector(".repository-send-redline");
      setPanelMessage("");
      if (sendButton) {
        sendButton.disabled = true;
        sendButton.textContent = "Sending";
      }
      try {
        const response = await fetch("/api/gmail/send-redline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ matter_id: matter.id, confirm_send: true }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Redline email could not send");
        pendingSendMatterId = null;
        if (payload.matter?.id) {
          replaceMatter(payload.matter);
          renderBoard();
          renderDetailPanel(payload.matter);
        }
        setPanelMessage(`Sent redline to ${recipient}.`);
      } catch (error) {
        pendingSendMatterId = null;
        renderDetailPanel(matter);
        setPanelMessage(error.message || "Redline email could not send");
      }
    }

    async function closeMatterWorkflow(matter) {
      pendingSendMatterId = null;
      const closeButton = repositoryMatterPanel?.querySelector(".repository-close-matter");
      setPanelMessage("");
      if (closeButton) {
        closeButton.disabled = true;
        closeButton.textContent = "Closing";
      }
      const movedMatter = await moveMatterToColumn(matter.id, "signed_closed", { quiet: true });
      setPanelMessage(movedMatter ? "Moved to Signed / Closed." : "Matter could not move.");
      if (closeButton?.isConnected && !movedMatter) {
        closeButton.disabled = false;
        closeButton.textContent = "Close Matter";
      }
    }

    async function markMatterRedlineReady(matter) {
      if (!matter?.id) return null;
      return moveMatterToColumn(matter.id, "redline_ready", { quiet: true });
    }

    async function moveMatterToColumn(matterId, boardColumn, options = {}) {
      try {
        const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/stage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ board_column: boardColumn }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Matter could not move");
        const updatedMatter = payload.matter;
        if (!updatedMatter?.id) throw new Error("Matter could not move");
        replaceMatter(updatedMatter);
        renderBoard();
        if (options.renderPanel !== false && selectedMatter?.id === updatedMatter.id) {
          renderDetailPanel(selectedMatter);
        }
        return updatedMatter;
      } catch (error) {
        if (!options.quiet) {
          setPanelMessage(error.message || "Matter could not move");
        }
        return null;
      }
    }

    function replaceMatter(updatedMatter) {
      if (!updatedMatter?.id) return;
      const matterIndex = state.matters.findIndex((matter) => matter.id === updatedMatter.id);
      state.matters = matterIndex >= 0
        ? state.matters.map((matter) => (matter.id === updatedMatter.id ? updatedMatter : matter))
        : [updatedMatter, ...state.matters];
      if (selectedMatter?.id === updatedMatter.id) {
        selectedMatter = updatedMatter;
      }
      if (state.selectedMatter?.id === updatedMatter.id) {
        state.selectedMatter = updatedMatter;
      }
    }

    function setPanelMessage(message) {
      const messageNode = repositoryMatterPanel?.querySelector(".repository-detail-message");
      if (messageNode) messageNode.textContent = message;
    }

    function setImportStatus(message) {
      if (repositoryImportStatus) repositoryImportStatus.textContent = message;
    }

    function renderGmailStatus(status) {
      const inboundNode = gmailDemoStatus?.querySelector('[data-gmail-role="inbound"]');
      const outboundNode = gmailDemoStatus?.querySelector('[data-gmail-role="outbound"]');
      renderGmailAccountStatus(inboundNode, status.inbound);
      renderGmailAccountStatus(outboundNode, status.outbound);
    }

    function renderGmailAccountStatus(node, account) {
      if (!node) return;
      node.classList.toggle("ready", Boolean(account?.ready));
      node.classList.toggle("blocked", !account?.ready);
      node.textContent = account?.ready ? (account.email || "Connected") : (account?.error || "Not connected");
    }

    function updateLastSync(account) {
      if (!gmailLastSync) return;
      const label = formatMatterDateTime(new Date().toISOString()) || "Just now";
      gmailLastSync.textContent = account ? `${label} (${account})` : label;
    }

    function gmailSyncSummary(imported, skipped) {
      if (imported.length) {
        const skippedText = skippedReasonSummary(skipped);
        return skippedText ? `Imported ${imported.length} from Gmail; ${skippedText}` : `Imported ${imported.length} from Gmail`;
      }
      const skippedText = skippedReasonSummary(skipped);
      return skippedText ? `No new imports; ${skippedText}` : "No new Gmail attachments";
    }

    function skippedReasonSummary(skipped) {
      if (!skipped.length) return "";
      const counts = skipped.reduce((totals, item) => {
        const label = skippedReasonLabel(item?.reason);
        totals[label] = (totals[label] || 0) + 1;
        return totals;
      }, {});
      const details = Object.entries(counts)
        .map(([label, count]) => `${count} ${label}`)
        .join(", ");
      return `skipped ${skipped.length} (${details})`;
    }

    return { importMatter, loadGmailStatus, loadMatters, markMatterRedlineReady, openMatter, renderBoard, setImportStatus };
  }

  function renderMatterCard(matter) {
    const issueCount = Number(matter.issue_count || 0);
    const date = formatMatterDate(matter.received_at || matter.created_at);
    return `
      <button class="repository-card" type="button" data-matter-id="${escapeHtml(matter.id)}">
        <span class="repository-card-top">
          <span class="repository-card-badges">
            <span class="repository-priority">${escapeHtml(triageLabel(matter.triage_status))}</span>
            <span class="repository-source-badge ${escapeHtml(sourceBadgeClass(matter.source_type))}">${escapeHtml(sourceTypeLabel(matter.source_type))}</span>
          </span>
          <span>${escapeHtml(date)}</span>
        </span>
        <strong>${escapeHtml(matterSubject(matter))}</strong>
        <span class="repository-card-source">${escapeHtml(matterSender(matter))}</span>
        <span class="repository-card-snippet">${escapeHtml(matter.message_snippet || matter.attachment_filename || matter.source_filename || sourceTypeLabel(matter.source_type))}</span>
        <span class="repository-card-rule"></span>
        <span class="repository-card-foot">
          <span>${issueCount} ${issueCount === 1 ? "issue" : "issues"}</span>
          <span>${escapeHtml(boardColumnLabel(matter.board_column))}</span>
        </span>
      </button>
    `;
  }

  function triageLabel(status) {
    const labels = {
      ready_to_sign: "Ready",
      needs_redline: "Redline",
      legal_review: "Legal",
      intake_error: "Error",
    };
    return labels[status] || "Review";
  }

  function renderFailedClauses(clauses) {
    if (!clauses.length) {
      return '<p class="repository-detail-none">No failed clauses</p>';
    }
    return `
      <ul>
        ${clauses.slice(0, 6).map((clause) => `
          <li>
            <strong>${escapeHtml(clause.name || clause.id || "Clause")}</strong>
            <span>${escapeHtml(clause.issue_label || clause.reason || "Needs review")}</span>
          </li>
        `).join("")}
      </ul>
    `;
  }

  function sourceTypeLabel(sourceType) {
    const labels = {
      gmail_demo: "Gmail Demo",
      gmail_inbound: "Gmail Inbound",
    };
    return labels[sourceType] || sourceType || "Source";
  }

  function sourceBadgeClass(sourceType) {
    return sourceType === "gmail_inbound" ? "inbound" : "demo";
  }

  function skippedReasonLabel(reason) {
    const labels = {
      attachment_too_large: "too large",
      attachment_unavailable: "attachment unavailable",
      duplicate_attachment: "duplicate",
      message_unavailable: "message unavailable",
      no_docx_attachment: "no DOCX",
      no_reviewable_attachment: "no DOCX/PDF",
      review_failed: "review failed",
    };
    return labels[reason] || "skipped";
  }

  function isReviewableDocument(filename) {
    const lowerFilename = String(filename || "").toLowerCase();
    return lowerFilename.endsWith(".docx") || lowerFilename.endsWith(".pdf");
  }

  function boardColumnLabel(boardColumn) {
    return BOARD_COLUMNS.find((column) => column.id === boardColumn)?.label || "Gmail Demo";
  }

  function matterSubject(matter) {
    return matter.subject || matter.document_title || matter.source_filename || "Untitled NDA";
  }

  function matterSender(matter) {
    return matter.sender || sourceTypeLabel(matter.source_type);
  }

  function documentTitleFromFilename(filename) {
    return (filename.split(/[\\/]/).pop() || filename).replace(/\.[^.]*$/, "") || "Untitled NDA";
  }

  function formatMatterDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  }

  function formatMatterDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString(undefined, {
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
    });
  }

  return { boardColumnLabel, createController, formatMatterDate, renderMatterCard, sourceTypeLabel, triageLabel };
})();

function createRepositoryController(options) {
  return RepositoryView.createController(options);
}
