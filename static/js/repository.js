const RepositoryView = (() => {
  function createController({
    state,
    fileInput,
    repositoryFileInput,
    gmailDemoMatterList,
    repositoryImportStatus,
    fileToBase64,
    loadMatterIntoReview,
    reviewErrorFromPayload,
  }) {
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
        card.addEventListener("click", () => openMatter(card.dataset.matterId));
      });
    }

    async function openMatter(matterId) {
      try {
        const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}`);
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Matter could not load");
        loadMatterIntoReview(payload.matter);
        if (fileInput) fileInput.value = "";
      } catch (error) {
        setImportStatus(error.message || "Matter could not load");
      }
    }

    function setImportStatus(message) {
      if (repositoryImportStatus) repositoryImportStatus.textContent = message;
    }

    return { importMatter, loadMatters, renderBoard, setImportStatus };
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

  function sourceTypeLabel(sourceType) {
    const labels = {
      gmail_demo: "Gmail Demo",
      manual_upload: "Manual upload",
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
