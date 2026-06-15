// Corpus tab (read-only filing-cabinet view).
//
// Mirrors the Repository FE split: a pure CorpusModel (label/date helpers), a
// CorpusRender render module, and a thin controller that fetches GET /api/corpus
// and paints the grouped Counterparty -> Contract (matter) -> lifecycle-artifact
// tree. There are NO write actions here — every control is a read or a link-out.
const CorpusModel = (() => {
  // role -> lifecycle stage label, matching corpus_index's role mapping.
  // The backend already supplies stage_label per artifact; this is the fallback
  // when an artifact arrives without one.
  const ROLE_STAGE_LABELS = {
    original: "received",
    redline: "ai_redline",
    reviewed: "legal_review",
    generated: "draft",
    counter: "counter",
  };

  const SOURCE_LABELS = {
    app: "In app",
    drive: "On file (Drive)",
    both: "In app + Drive",
  };

  function artifactStageLabel(artifact) {
    if (!artifact || typeof artifact !== "object") return "";
    if (artifact.stage_label) return String(artifact.stage_label);
    return ROLE_STAGE_LABELS[artifact.role] || String(artifact.role || "");
  }

  function sourceLabel(source) {
    return SOURCE_LABELS[source] || "";
  }

  function counterpartyName(group) {
    return (group && group.counterparty) || "Unknown counterparty";
  }

  function matterTitle(matter) {
    return (matter && matter.title) || "NDA";
  }

  // Reuse the Repository date helper when present so Corpus and Repository render
  // identical date formatting; fall back to a local formatter otherwise.
  function formatDate(value) {
    if (typeof RepositoryModel !== "undefined" && typeof RepositoryModel.formatMatterDate === "function") {
      return RepositoryModel.formatMatterDate(value);
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  }

  function stageBadge(matter) {
    return (matter && matter.stage) || "On file";
  }

  function artifactCountLabel(count) {
    const value = Number(count || 0);
    return `${value} ${value === 1 ? "file" : "files"}`;
  }

  return {
    ROLE_STAGE_LABELS,
    artifactCountLabel,
    artifactStageLabel,
    counterpartyName,
    formatDate,
    matterTitle,
    sourceLabel,
    stageBadge,
  };
})();

const CorpusRender = (() => {
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

  function renderDriveStatus(node, drive) {
    if (!node) return;
    const info = drive || {};
    node.classList.remove("error", "ok", "warn");
    if (!info.connected) {
      node.classList.add("warn");
      node.textContent = "Drive not connected — showing in-app records only";
      return;
    }
    if (!info.reconciled) {
      node.classList.add("warn");
      const reasons = {
        not_connected: "Drive not connected",
        drive_error: "Drive listing failed",
        rate_limited: "Drive rate-limited — retry shortly",
        drive_skipped: "Drive crawl skipped",
      };
      node.textContent = `${reasons[info.reason] || "Drive not reconciled"} — showing in-app records`;
      return;
    }
    node.classList.add("ok");
    const parts = ["Drive reconciled"];
    if (info.from_cache) parts.push("cached");
    if (info.stale) {
      node.classList.add("warn");
      parts.push("stale — refresh to update");
    }
    if (info.built_at) {
      const built = CorpusModel.formatDate(info.built_at);
      if (built) parts.push(`as of ${built}`);
    }
    node.textContent = parts.join(" · ");
  }

  function renderGroups(listNode, payload, handlers) {
    if (!listNode) return;
    const groups = Array.isArray(payload.groups) ? payload.groups : [];
    listNode.innerHTML = groups.map((group) => renderGroup(group)).join("");
    bindEvents(listNode, handlers);
  }

  function renderGroup(group) {
    const matters = Array.isArray(group.matters) ? group.matters : [];
    const matterCount = Number(group.matter_count ?? matters.length);
    return `
      <section class="corpus-group">
        <header class="corpus-group-head">
          <h2 class="corpus-group-name">${html(CorpusModel.counterpartyName(group))}</h2>
          <span class="corpus-group-count">${matterCount} ${matterCount === 1 ? "matter" : "matters"}</span>
        </header>
        <div class="corpus-matter-list">
          ${matters.map((matter) => renderMatter(matter)).join("")}
        </div>
      </section>
    `;
  }

  function renderMatter(matter) {
    const artifacts = Array.isArray(matter.artifacts) ? matter.artifacts : [];
    const artifactCount = Number(matter.artifact_count ?? artifacts.length);
    const key = matter.matter_id || matter.open_in_drive_url || CorpusModel.matterTitle(matter);
    const date = CorpusModel.formatDate(matter.created_at);
    const inApp = Boolean(matter.in_app);
    const driveUrl = matter.open_in_drive_url || "";
    return `
      <article class="corpus-matter" data-corpus-matter="${html(key)}">
        <header class="corpus-matter-head" role="button" tabindex="0" data-corpus-toggle="${html(key)}" aria-expanded="false">
          <span class="corpus-matter-disclosure" aria-hidden="true">▸</span>
          <span class="corpus-matter-main">
            <strong class="corpus-matter-title">${html(CorpusModel.matterTitle(matter))}</strong>
            <span class="corpus-matter-sub">${html(CorpusModel.counterpartyName(matter))}</span>
          </span>
          <span class="corpus-matter-meta">
            ${date ? `<span class="corpus-matter-date">${html(date)}</span>` : ""}
            <span class="corpus-stage-badge" title="Lifecycle stage">${html(CorpusModel.stageBadge(matter))}</span>
            ${matter.source ? `<span class="corpus-source-badge corpus-source-${html(matter.source)}" title="${html(CorpusModel.sourceLabel(matter.source))}">${html(CorpusModel.sourceLabel(matter.source) || matter.source)}</span>` : ""}
            ${matter.duplicate ? `<span class="corpus-duplicate-chip" title="More than one Drive folder maps to this matter">DUPLICATE</span>` : ""}
            <span class="corpus-artifact-count">${html(CorpusModel.artifactCountLabel(artifactCount))}</span>
          </span>
        </header>
        <div class="corpus-matter-body" hidden>
          <div class="corpus-matter-actions">
            ${driveUrl ? `<a class="corpus-link" href="${html(driveUrl)}" target="_blank" rel="noopener noreferrer">Open in Drive</a>` : ""}
            <button class="corpus-link corpus-open-matter" type="button" data-corpus-open-matter="${html(matter.matter_id || "")}" ${inApp && matter.matter_id ? "" : "disabled"} title="${inApp ? "Open this matter in the workstation" : "Not available in app (Drive-only)"}">Open matter</button>
          </div>
          ${renderDuplicateNote(matter)}
          ${renderArtifacts(matter, artifacts)}
        </div>
      </article>
    `;
  }

  function renderDuplicateNote(matter) {
    if (!matter.duplicate) return "";
    const urls = Array.isArray(matter.duplicate_folder_urls) ? matter.duplicate_folder_urls : [];
    if (!urls.length) return "";
    const links = urls
      .map((url, index) => `<a class="corpus-link" href="${html(url)}" target="_blank" rel="noopener noreferrer">Duplicate folder ${index + 1}</a>`)
      .join("");
    return `<div class="corpus-duplicate-note">Extra Drive folder(s) detected: ${links}</div>`;
  }

  function renderArtifacts(matter, artifacts) {
    if (!artifacts.length) {
      return `<div class="corpus-artifact-empty">No filed documents</div>`;
    }
    const rows = artifacts.map((artifact) => renderArtifactRow(matter, artifact)).join("");
    return `<ol class="corpus-artifact-list">${rows}</ol>`;
  }

  function renderArtifactRow(matter, artifact) {
    const stage = CorpusModel.artifactStageLabel(artifact);
    const date = CorpusModel.formatDate(artifact.created_at);
    const download = artifact.download_url || "";
    const driveFile = artifact.drive_file_url || "";
    let action = "";
    if (download) {
      action = `<a class="corpus-link corpus-artifact-download" href="${html(download)}" download>Download</a>`;
    } else if (driveFile) {
      action = `<a class="corpus-link" href="${html(driveFile)}" target="_blank" rel="noopener noreferrer">View in Drive</a>`;
    }
    const seq = Number(artifact.sequence || 0);
    return `
      <li class="corpus-artifact">
        <span class="corpus-artifact-seq" aria-hidden="true">${seq ? html(String(seq)) : "•"}</span>
        <span class="corpus-artifact-main">
          <span class="corpus-artifact-name">${html(artifact.filename || artifact.role || "Document")}</span>
          <span class="corpus-artifact-sub">
            ${stage ? `<span class="corpus-artifact-stage">${html(stage)}</span>` : ""}
            ${artifact.actor ? `<span class="corpus-artifact-actor">${html(artifact.actor)}</span>` : ""}
            ${artifact.version ? `<span class="corpus-artifact-version">v${html(String(artifact.version))}</span>` : ""}
            ${date ? `<span class="corpus-artifact-date">${html(date)}</span>` : ""}
          </span>
        </span>
        <span class="corpus-artifact-action">${action}</span>
      </li>
    `;
  }

  function bindEvents(listNode, handlers) {
    listNode.querySelectorAll("[data-corpus-toggle]").forEach((head) => {
      const toggle = () => {
        const article = head.closest(".corpus-matter");
        const body = article?.querySelector(".corpus-matter-body");
        if (!body) return;
        const expanded = head.getAttribute("aria-expanded") === "true";
        head.setAttribute("aria-expanded", expanded ? "false" : "true");
        body.hidden = expanded;
        const disclosure = head.querySelector(".corpus-matter-disclosure");
        if (disclosure) disclosure.textContent = expanded ? "▸" : "▾";
      };
      head.addEventListener("click", (event) => {
        // Let action links/buttons inside the body work without toggling.
        if (event.target.closest("a, button")) return;
        toggle();
      });
      head.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        toggle();
      });
    });
    listNode.querySelectorAll("[data-corpus-open-matter]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const matterId = button.dataset.corpusOpenMatter;
        if (matterId && handlers && typeof handlers.openMatter === "function") {
          handlers.openMatter(matterId);
        }
      });
    });
  }

  return { renderDriveStatus, renderGroups };
})();

const CorpusView = (() => {
  function fetchCorpus({ refresh = false } = {}) {
    const url = refresh ? "/api/corpus?refresh=1" : "/api/corpus";
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } }).then((response) => {
      if (!response.ok) {
        const error = new Error(`Corpus request failed (${response.status})`);
        error.status = response.status;
        throw error;
      }
      return response.json();
    });
  }

  function createController({ panel, listNode, emptyNode, statusNode, summaryNode, refreshButton, openMatter }) {
    let loadedOnce = false;
    let loading = false;

    function setLoading(isLoading) {
      loading = isLoading;
      if (refreshButton) refreshButton.disabled = isLoading;
      if (panel) panel.classList.toggle("loading", isLoading);
    }

    function renderEmptyState(message) {
      if (!emptyNode) return;
      emptyNode.hidden = false;
      emptyNode.textContent = message;
      if (listNode) listNode.innerHTML = "";
    }

    function clearEmptyState() {
      if (emptyNode) emptyNode.hidden = true;
    }

    function renderSummary(payload) {
      if (!summaryNode) return;
      const matters = Number(payload.matter_count || 0);
      const counterparties = Number(payload.counterparty_count || 0);
      summaryNode.textContent = `${counterparties} ${counterparties === 1 ? "counterparty" : "counterparties"} · ${matters} ${matters === 1 ? "matter" : "matters"}`;
    }

    function render(payload) {
      const groups = Array.isArray(payload.groups) ? payload.groups : [];
      CorpusRender.renderDriveStatus(statusNode, payload.drive);
      renderSummary(payload);
      if (!groups.length) {
        renderEmptyState("No NDAs on file yet.");
        return;
      }
      clearEmptyState();
      CorpusRender.renderGroups(listNode, payload, { openMatter });
    }

    function load({ refresh = false } = {}) {
      if (loading) return Promise.resolve();
      setLoading(true);
      if (!loadedOnce && emptyNode) {
        renderEmptyState("Loading corpus…");
      }
      return fetchCorpus({ refresh })
        .then((payload) => {
          loadedOnce = true;
          render(payload || {});
        })
        .catch((error) => {
          renderEmptyState(
            error && error.status === 401
              ? "Sign in to view your corpus."
              : "Could not load corpus. Try refresh."
          );
          if (statusNode) {
            statusNode.classList.remove("ok");
            statusNode.classList.add("error");
            statusNode.textContent = "Corpus unavailable";
          }
        })
        .finally(() => setLoading(false));
    }

    if (refreshButton) {
      refreshButton.addEventListener("click", () => load({ refresh: true }));
    }

    return {
      load,
      refresh: () => load({ refresh: true }),
    };
  }

  return { createController, fetchCorpus };
})();

function createCorpusController(options) {
  return CorpusView.createController(options);
}
