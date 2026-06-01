const RepositoryView = (() => {
  const BOARD_COLUMNS = [
    { id: "gmail_demo", label: "Gmail Demo" },
    { id: "in_review", label: "In Review" },
    { id: "redline_ready", label: "Redline Ready" },
    { id: "signed_closed", label: "Signed / Closed" },
  ];

  function createController({
    state,
    gmailDemoMatterList,
    repositoryMatterPanel,
    downloadBlob,
    downloadFilename,
    loadMatterIntoReview,
    redlineDownloadFilename,
    reviewErrorFromPayload,
  }) {
    let selectedMatter = null;
    let pendingSendMatterId = null;
    const repositoryWorkspace = repositoryMatterPanel?.closest(".repository-workspace");
    const boardColumnIds = new Set(BOARD_COLUMNS.map((column) => column.id));

    repositoryMatterPanel?.addEventListener("click", (event) => {
      if (event.target === repositoryMatterPanel) closePanel();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && repositoryMatterPanel && !repositoryMatterPanel.hidden) closePanel();
    });

    async function loadGmailStatus() {
      try {
        const response = await fetch("/api/gmail/status");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail status could not load");
        state.gmailStatus = payload.gmail || {};
        renderSyncStatus();
      } catch (error) {
        state.gmailStatus = {
          inbound: { ready: false, error: error.message || "Status unavailable" },
          outbound: { ready: false, error: error.message || "Status unavailable" },
        };
        renderSyncStatus();
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
      mattersByColumn.forEach((matters) => matters.sort(compareMatterRecency));
      document.querySelectorAll("[data-repository-count]").forEach((count) => {
        count.textContent = String(mattersByColumn.get(count.dataset.repositoryCount)?.length || 0);
      });
      renderSyncStatus();
      document.querySelectorAll("[data-repository-list]").forEach((list) => {
        const matters = mattersByColumn.get(list.dataset.repositoryList) || [];
        list.innerHTML = matters.length
          ? matters.map(renderMatterCard).join("")
          : '<div class="repository-dropzone">No documents</div>';
        list.querySelectorAll("[data-matter-id]").forEach((card) => {
          card.classList.toggle("active", card.dataset.matterId === selectedMatter?.id);
          card.addEventListener("click", () => openMatter(card.dataset.matterId));
          card.addEventListener("keydown", (event) => {
            if (event.target !== card || (event.key !== "Enter" && event.key !== " ")) return;
            event.preventDefault();
            openMatter(card.dataset.matterId);
          });
        });
        list.querySelectorAll("[data-delete-matter-id]").forEach((button) => {
          button.addEventListener("click", (event) => {
            event.stopPropagation();
            deleteMatter(button.dataset.deleteMatterId, button);
          });
        });
      });
    }

    function renderSyncStatus() {
      const node = document.querySelector("[data-repository-sync-status]");
      if (!node) return;
      const settings = state.gmailStatus?.settings || {};
      const recentRun = Array.isArray(settings.sync_history) ? settings.sync_history[0] : null;
      node.classList.toggle("error", recentRun?.status === "error");
      if (recentRun?.status === "error") {
        node.textContent = `Last sync error: ${recentRun.error || "check Admin"}`;
        return;
      }
      if (!settings.last_sync_at) {
        node.textContent = "Waiting for scheduled sync";
        return;
      }
      const imported = Number(settings.last_sync_imported_count || 0);
      const skipped = Number(settings.last_sync_skipped_count || 0);
      node.textContent = `Last sync ${formatMatterDateTime(settings.last_sync_at) || settings.last_sync_at} - ${imported} imported / ${skipped} skipped`;
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
        console.warn(error.message || "Matter could not load");
      }
    }

    async function deleteMatter(matterId, control) {
      if (!matterId) return;
      pendingSendMatterId = null;
      if (control) {
        control.disabled = true;
        control.setAttribute("aria-busy", "true");
      }
      try {
        const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}`, { method: "DELETE" });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Matter could not be deleted");
        state.matters = state.matters.filter((matter) => matter.id !== matterId);
        if (selectedMatter?.id === matterId) {
          selectedMatter = null;
          renderEmptyPanel();
        }
        if (state.selectedMatter?.id === matterId) {
          state.selectedMatter = null;
        }
        renderBoard();
      } catch (error) {
        if (control?.isConnected) {
          control.disabled = false;
          control.removeAttribute("aria-busy");
        }
        if (repositoryMatterPanel && !repositoryMatterPanel.hidden) {
          setPanelMessage(error.message || "Matter could not be deleted");
        } else {
          console.warn(error.message || "Matter could not be deleted");
        }
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
      const fileName = matter.source_filename || matter.attachment_filename || subject;
      const recipient = MatterUtils.recipientEmail(matter);
      const sendBlockReason = MatterUtils.gmailSendBlock(matter, state.gmailStatus);
      const sendBlockLabel = MatterUtils.gmailSendButtonLabel(sendBlockReason);
      const canSendRedline = !sendBlockReason;
      const confirmingSend = pendingSendMatterId === matter.id;
      repositoryMatterPanel.hidden = false;
      repositoryWorkspace?.classList.remove("detail-open");
      repositoryMatterPanel.setAttribute("aria-label", `Matter inspector for ${fileName}`);
      repositoryMatterPanel.innerHTML = `
        <section class="repository-inspector-dialog" aria-labelledby="repositoryInspectorTitle">
          <header class="repository-detail-head">
            <div class="repository-inspector-heading">
              <span class="repository-inspector-icon" aria-hidden="true"></span>
              <div>
                <p class="repository-detail-kicker">${escapeHtml(sourceTypeLabel(matter.source_type))}</p>
                <h2 id="repositoryInspectorTitle">${escapeHtml(fileName)}</h2>
              </div>
            </div>
            <button class="repository-detail-close" type="button" aria-label="Close matter inspector">x</button>
          </header>

          <div class="repository-inspector-body">
            <section class="repository-inspector-main" aria-label="Matter review details">
              <section class="repository-inspector-section">
                <p class="repository-inspector-section-title">Metadata Details</p>
                <dl class="repository-detail-meta repository-detail-meta-grid">
                  ${renderInspectorField("File name", fileName)}
                  ${renderInspectorField("Subject", subject)}
                  ${renderInspectorField("Status", boardColumnLabel(matter.board_column))}
                  ${renderInspectorField("Review route", triageLabel(matter.triage_status))}
                  ${renderInspectorField("Date ingested", formatMatterDateTime(matter.created_at) || "-")}
                  ${renderInspectorField("Last updated", formatMatterDateTime(matter.updated_at) || "-")}
                </dl>
              </section>

              <section class="repository-inspector-section">
                <p class="repository-inspector-section-title">Review Checks</p>
                <div class="repository-check-grid">
                  <div class="repository-check-card">
                    <span>Pass checks</span>
                    <strong>${Number(matter.requirements_passed || 0)} passed / ${Number(matter.requirements_failed || 0)} failed</strong>
                  </div>
                  <div class="repository-check-card">
                    <span>Playbook match</span>
                    <strong>${escapeHtml(playbookMatchLabel(matter, reviewResult))}</strong>
                  </div>
                  <div class="repository-check-card">
                    <span>Issues</span>
                    <strong>${Number(matter.issue_count || 0)} ${Number(matter.issue_count || 0) === 1 ? "issue" : "issues"}</strong>
                  </div>
                  <div class="repository-check-card">
                    <span>Redline draft</span>
                    <strong>${escapeHtml(matter.has_redline_draft ? "Draft redline saved" : "No custom draft")}</strong>
                  </div>
                </div>
              </section>

              <section class="repository-detail-issues">
                <h3>Key Failed Clauses</h3>
                ${renderFailedClauses(failedClauses)}
              </section>

              ${renderSendComposer(matter, recipient, confirmingSend)}
            </section>

            <aside class="repository-inspector-side" aria-label="Matter routing and timeline">
              <section class="repository-inspector-section">
                <p class="repository-inspector-section-title">Gmail Routing</p>
                <dl class="repository-detail-email">
                  ${renderInspectorField("From", matterSender(matter))}
                  ${renderInspectorField("Inbound mailbox", matter.gmail_account || "Manual repository intake")}
                  ${renderInspectorField("Outbound status", sendBlockReason || "Ready")}
                  ${renderInspectorField("Reply to", recipient || "No reply address detected")}
                  ${renderInspectorField("Received", formatMatterDateTime(matter.received_at || matter.created_at) || "-")}
                  ${renderInspectorField("Attachment", matter.attachment_filename || matter.source_filename || "-")}
                  ${matter.last_outbound_at ? renderInspectorField("Last sent", formatMatterDateTime(matter.last_outbound_at) || "-") : ""}
                  ${matter.last_outbound_account ? renderInspectorField("Last sent from", matter.last_outbound_account) : ""}
                  ${matter.last_outbound_to ? renderInspectorField("Last sent to", matter.last_outbound_to) : ""}
                </dl>
                <p class="repository-message-preview">${escapeHtml(matter.message_snippet || "No message preview available.")}</p>
              </section>

              <section class="repository-inspector-section">
                <p class="repository-inspector-section-title">Matter Timeline</p>
                ${renderMatterTimeline(matter)}
              </section>
            </aside>
          </div>

          <footer class="repository-inspector-footer">
            <p class="repository-detail-message" aria-live="polite"></p>
            <div class="repository-detail-actions">
              <button type="button" class="repository-open-review">Open Review</button>
              <button type="button" class="secondary repository-export-redline">Export Redline</button>
              <button type="button" class="secondary repository-send-redline ${confirmingSend ? "confirming" : ""}" ${canSendRedline ? "" : "disabled"} title="${escapeHtml(sendBlockReason)}">${sendBlockReason ? escapeHtml(sendBlockLabel) : confirmingSend ? "Confirm Send" : "Send Redline"}</button>
              <button type="button" class="secondary repository-close-matter" ${isClosed ? "disabled" : ""}>Close Matter</button>
            </div>
          </footer>
        </section>
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
      const reviewMatter = await loadMatterReview(selectedMatter.id);
      if (!reviewMatter) {
        setPanelMessage("Matter review details could not load.");
        return;
      }
      loadMatterIntoReview(reviewMatter);
    }

    async function loadMatterReview(matterId) {
      try {
        const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/review`);
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Matter review details could not load");
        return {
          ...(payload.matter || {}),
          extracted_text: payload.extracted_text || "",
          redline_draft: payload.redline_draft || null,
          review_result: payload.review_result || {},
        };
      } catch (error) {
        console.warn(error.message || "Matter review details could not load");
        return null;
      }
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
      const sendBlockReason = MatterUtils.gmailSendBlock(matter, state.gmailStatus);
      if (sendBlockReason) {
        pendingSendMatterId = null;
        renderDetailPanel(matter);
        setPanelMessage(sendBlockReason);
        return;
      }
      const recipient = MatterUtils.recipientEmail(matter);
      if (!recipient) {
        pendingSendMatterId = null;
        setPanelMessage("Matter does not have a valid reply recipient email address.");
        return;
      }
      if (pendingSendMatterId !== matter.id) {
        pendingSendMatterId = matter.id;
        renderDetailPanel(matter);
        setPanelMessage("Review outbound email details, then confirm send.");
        return;
      }

      const sendButton = repositoryMatterPanel?.querySelector(".repository-send-redline");
      const subject = repositoryMatterPanel?.querySelector("#repositorySendSubject")?.value || "";
      const body = repositoryMatterPanel?.querySelector("#repositorySendBody")?.value || "";
      const sendPayload = {
        matter_id: matter.id,
        confirm_send: true,
      };
      if (subject.trim()) sendPayload.subject = subject;
      if (body.trim()) sendPayload.body = body;
      setPanelMessage("");
      if (sendButton) {
        sendButton.disabled = true;
        sendButton.textContent = "Sending";
      }
      try {
        const response = await fetch("/api/gmail/send-redline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sendPayload),
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

    function renderSendComposer(matter, recipient, confirmingSend) {
      if (!confirmingSend) return "";
      const subject = defaultOutboundSubject(matter);
      const body = defaultOutboundBody(matter);
      return `
        <section class="repository-send-composer" aria-label="Outbound redline email">
          <dl class="repository-send-route">
            <div>
              <dt>From</dt>
              <dd>${escapeHtml(outboundAccountLabel())}</dd>
            </div>
            <div>
              <dt>To</dt>
              <dd>${escapeHtml(recipient)}</dd>
            </div>
          </dl>
          <label class="repository-send-field" for="repositorySendSubject">
            <span>Subject</span>
            <input id="repositorySendSubject" type="text" value="${escapeHtml(subject)}" autocomplete="off">
          </label>
          <label class="repository-send-field" for="repositorySendBody">
            <span>Message</span>
            <textarea id="repositorySendBody" rows="7">${escapeHtml(body)}</textarea>
          </label>
        </section>
      `;
    }

    function outboundAccountLabel() {
      const outbound = state.gmailStatus?.outbound || {};
      if (outbound.ready && outbound.email) return outbound.email;
      return outbound.error || outbound.email || "Outbound Gmail not connected";
    }

    function defaultOutboundSubject(matter) {
      const subject = String(matter.subject || matter.document_title || matter.source_filename || "NDA redline").trim();
      if (!subject) return "Re: NDA redline";
      return subject.toLowerCase().startsWith("re:") ? subject : `Re: ${subject}`;
    }

    function defaultOutboundBody(matter) {
      const subject = matter.subject || matter.document_title || matter.source_filename || "the NDA";
      return `Hi,\n\nPlease find attached the redlined version of ${subject}.\n\nBest,\nAspora Legal`;
    }

    return {
      loadGmailStatus,
      loadMatters,
      markMatterRedlineReady,
      openMatter,
      renderBoard,
    };
  }

  function renderMatterCard(matter) {
    const issueCount = Number(matter.issue_count || 0);
    const date = formatMatterDate(matter.received_at || matter.created_at);
    return `
      <article class="repository-card" role="button" tabindex="0" data-matter-id="${escapeHtml(matter.id)}" aria-label="Open matter ${escapeHtml(matterSubject(matter))}">
        <span class="repository-card-top">
          <span class="repository-card-badges">
            <span class="repository-priority">${escapeHtml(triageLabel(matter.triage_status))}</span>
            <span class="repository-source-badge ${escapeHtml(sourceBadgeClass(matter.source_type))}">${escapeHtml(sourceTypeLabel(matter.source_type))}</span>
          </span>
          <span class="repository-card-top-actions">
            <span>${escapeHtml(date)}</span>
            <button class="repository-card-delete" type="button" data-delete-matter-id="${escapeHtml(matter.id)}" aria-label="Delete matter" title="Delete matter">x</button>
          </span>
        </span>
        <strong>${escapeHtml(matterSubject(matter))}</strong>
        <span class="repository-card-source">${escapeHtml(matterSender(matter))}</span>
        <span class="repository-card-snippet">${escapeHtml(matter.message_snippet || matter.attachment_filename || matter.source_filename || sourceTypeLabel(matter.source_type))}</span>
        <span class="repository-card-rule"></span>
        <span class="repository-card-foot">
          <span>${issueCount} ${issueCount === 1 ? "issue" : "issues"}</span>
          <span>${escapeHtml(boardColumnLabel(matter.board_column))}</span>
        </span>
      </article>
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

  function renderInspectorField(label, value) {
    const displayValue = value === undefined || value === null || value === "" ? "-" : String(value);
    return `
      <div>
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(displayValue)}</dd>
      </div>
    `;
  }

  function playbookMatchLabel(matter, reviewResult) {
    const passed = Number(matter.requirements_passed ?? reviewResult.requirements_passed ?? 0);
    const failed = Number(matter.requirements_failed ?? reviewResult.requirements_failed ?? 0);
    const total = passed + failed;
    if (!total) return "Not checked";
    return `${Math.round((passed / total) * 100)}%`;
  }

  function renderMatterTimeline(matter) {
    const reviewResult = matter.review_result || {};
    const events = [
      {
        detail: matter.attachment_filename || matter.source_filename || "Document received for review.",
        meta: formatMatterDateTime(matter.received_at || matter.created_at) || "-",
        title: `${sourceTypeLabel(matter.source_type)} intake`,
      },
    ];
    if (reviewResult.checked_at) {
      events.push({
        detail: `${Number(matter.requirements_passed || 0)} checks passed, ${Number(matter.requirements_failed || 0)} failed.`,
        meta: formatMatterDateTime(reviewResult.checked_at) || "-",
        title: "Playbook checks completed",
      });
    }
    if (matter.has_redline_draft) {
      events.push({
        detail: "Custom redline decisions are saved for this matter.",
        meta: formatMatterDateTime(matter.updated_at) || "-",
        title: "Redline draft saved",
      });
    }
    if (matter.last_outbound_at) {
      events.push({
        detail: `Sent to ${matter.last_outbound_to || "counterparty"} from ${matter.last_outbound_account || "outbound Gmail"}.`,
        meta: formatMatterDateTime(matter.last_outbound_at) || "-",
        title: "Outbound redline sent",
      });
    }
    events.push({
      detail: matter.next_action || "Review matter and decide next step.",
      meta: boardColumnLabel(matter.board_column),
      title: "Current next action",
    });

    return `
      <ol class="repository-timeline">
        ${events.map((event) => `
          <li>
            <div>
              <strong>${escapeHtml(event.title)}</strong>
              <span>${escapeHtml(event.meta)}</span>
            </div>
            <p>${escapeHtml(event.detail)}</p>
          </li>
        `).join("")}
      </ol>
    `;
  }

  function sourceTypeLabel(sourceType) {
    const labels = {
      gmail_demo: "Gmail Demo",
      gmail_inbound: "Gmail Inbound",
      manual_upload: "Manual Upload",
    };
    return labels[sourceType] || sourceType || "Source";
  }

  function sourceBadgeClass(sourceType) {
    if (sourceType === "gmail_inbound") return "inbound";
    if (sourceType === "manual_upload") return "manual";
    return "demo";
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

  function compareMatterRecency(left, right) {
    return matterTimeValue(right) - matterTimeValue(left);
  }

  function matterTimeValue(matter) {
    const timestamp = Date.parse(matter.received_at || matter.created_at || matter.updated_at || "");
    return Number.isNaN(timestamp) ? 0 : timestamp;
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
