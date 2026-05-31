const RepositoryView = (() => {
  function createController({
    state,
    fileInput,
    repositoryFileInput,
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
    const repositoryWorkspace = repositoryMatterPanel?.closest(".repository-workspace");

    repositoryFileInput?.addEventListener("change", async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      await importMatter(file);
      repositoryFileInput.value = "";
    });

    async function importMatter(file) {
      if (!file.name.toLowerCase().endsWith(".docx")) {
        setImportStatus("Upload a .docx Word document");
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
      const gmailDemoMatters = state.matters.filter((matter) => matter.board_column === "gmail_demo");
      document.querySelectorAll("[data-repository-count]").forEach((count) => {
        const column = count.dataset.repositoryCount;
        count.textContent = column === "gmail_demo" ? String(gmailDemoMatters.length) : "0";
      });
      if (!gmailDemoMatterList) return;
      if (!gmailDemoMatters.length) {
        gmailDemoMatterList.innerHTML = '<div class="repository-dropzone">No documents</div>';
        return;
      }

      gmailDemoMatterList.innerHTML = gmailDemoMatters.map(renderMatterCard).join("");
      gmailDemoMatterList.querySelectorAll("[data-matter-id]").forEach((card) => {
        card.classList.toggle("active", card.dataset.matterId === selectedMatter?.id);
        card.addEventListener("click", () => openMatter(card.dataset.matterId));
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
        if (fileInput) fileInput.value = "";
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
      repositoryMatterPanel.hidden = false;
      repositoryWorkspace?.classList.add("detail-open");
      repositoryMatterPanel.innerHTML = `
        <header class="repository-detail-head">
          <div>
            <p class="repository-detail-kicker">${escapeHtml(sourceTypeLabel(matter.source_type))}</p>
            <h2>${escapeHtml(matter.document_title || matter.source_filename || "Untitled NDA")}</h2>
          </div>
          <button class="repository-detail-close" type="button" aria-label="Close matter panel">x</button>
        </header>
        <div class="repository-detail-status">
          <span class="repository-priority">${escapeHtml(triageLabel(matter.triage_status))}</span>
          <strong>${escapeHtml(matter.next_action || "Review")}</strong>
          <span>${Number(matter.issue_count || 0)} ${Number(matter.issue_count || 0) === 1 ? "issue" : "issues"}</span>
        </div>
        <dl class="repository-detail-meta">
          <div>
            <dt>Received</dt>
            <dd>${escapeHtml(formatMatterDate(matter.created_at) || "-")}</dd>
          </div>
          <div>
            <dt>File</dt>
            <dd>${escapeHtml(matter.source_filename || "-")}</dd>
          </div>
          <div>
            <dt>Requirements</dt>
            <dd>${Number(matter.requirements_passed || 0)} passed / ${Number(matter.requirements_failed || 0)} failed</dd>
          </div>
        </dl>
        <section class="repository-detail-issues">
          <h3>Key failed clauses</h3>
          ${renderFailedClauses(failedClauses)}
        </section>
        <div class="repository-detail-actions">
          <button type="button" class="repository-open-review">Open Review</button>
          <button type="button" class="secondary repository-export-redline">Export Redline</button>
        </div>
        <p class="repository-detail-message" aria-live="polite"></p>
      `;
      repositoryMatterPanel.querySelector(".repository-detail-close")?.addEventListener("click", closePanel);
      repositoryMatterPanel.querySelector(".repository-open-review")?.addEventListener("click", () => {
        loadMatterIntoReview(matter);
      });
      repositoryMatterPanel.querySelector(".repository-export-redline")?.addEventListener("click", () => exportMatter(matter));
    }

    function renderEmptyPanel() {
      if (!repositoryMatterPanel) return;
      repositoryWorkspace?.classList.remove("detail-open");
      repositoryMatterPanel.hidden = true;
      repositoryMatterPanel.innerHTML = '<div class="repository-detail-empty">Select a matter</div>';
    }

    function closePanel() {
      selectedMatter = null;
      renderEmptyPanel();
      renderBoard();
    }

    async function exportMatter(matter) {
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
        setPanelMessage(`Downloading ${filename}`);
      } catch (error) {
        setPanelMessage(error.message || "Export could not run");
      } finally {
        if (exportButton) {
          exportButton.disabled = false;
          exportButton.textContent = "Export Redline";
        }
      }
    }

    function setPanelMessage(message) {
      const messageNode = repositoryMatterPanel?.querySelector(".repository-detail-message");
      if (messageNode) messageNode.textContent = message;
    }

    function setImportStatus(message) {
      if (repositoryImportStatus) repositoryImportStatus.textContent = message;
    }

    return { importMatter, loadMatters, openMatter, renderBoard, setImportStatus };
  }

  function renderMatterCard(matter) {
    const issueCount = Number(matter.issue_count || 0);
    const date = formatMatterDate(matter.created_at);
    return `
      <button class="repository-card" type="button" data-matter-id="${escapeHtml(matter.id)}">
        <span class="repository-card-top">
          <span class="repository-priority">${escapeHtml(triageLabel(matter.triage_status))}</span>
          <span>${escapeHtml(date)}</span>
        </span>
        <strong>${escapeHtml(matter.document_title || matter.source_filename || "Untitled NDA")}</strong>
        <span class="repository-card-source">${escapeHtml(sourceTypeLabel(matter.source_type))}</span>
        <span class="repository-card-rule"></span>
        <span class="repository-card-foot">
          <span>${issueCount} ${issueCount === 1 ? "issue" : "issues"}</span>
          <span>${escapeHtml(matter.next_action || "Review")}</span>
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
    };
    return labels[sourceType] || sourceType || "Source";
  }

  function formatMatterDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  }

  return { createController, formatMatterDate, renderMatterCard, sourceTypeLabel, triageLabel };
})();

function createRepositoryController(options) {
  return RepositoryView.createController(options);
}
