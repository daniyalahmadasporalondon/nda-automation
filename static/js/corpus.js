// Corpus tab (read-only filing-cabinet view).
//
// Mirrors the Repository FE split: a pure CorpusModel (label/date/facet helpers),
// a CorpusRender render module, and a thin controller that fetches GET /api/corpus
// once and paints the grouped Counterparty -> Contract (matter) -> lifecycle-artifact
// tree, with a left facet rail + a token search bar that filter the *already-fetched*
// payload client-side (no refetch). There are NO write actions here — every control
// is a read or a link-out.
//
// Two axes, never blended:
//   * Axis A — STATUS chip = the Repository board column (RepositoryModel.boardColumnLabel),
//     value set = the 5 visible board columns (Generated/Inbox/In Review/Reviewed/Sent
//     + Upload). Drive-only matters with no board_column render "—".
//   * Axis B — LIFECYCLE rail = the per-matter artifact stages
//     (received -> draft -> ai_redline -> legal_review -> sent -> counter -> signed).
//   "On file" is a SOURCE state (the source badge), not a workflow status.
const CorpusModel = (() => {
  // role -> lifecycle stage label, matching corpus_index's _ROLE_STAGE_LABELS.
  // The backend already supplies stage_label per artifact; this is the fallback
  // when an artifact arrives without one. It must stay at full parity (all 7
  // roles) so the lifecycle rail can position artifacts on the fallback path.
  const ROLE_STAGE_LABELS = {
    original: "received",
    generated: "draft",
    redline: "ai_redline",
    reviewed: "legal_review",
    sent: "sent",
    counter: "counter",
    signed: "signed",
  };

  // Axis B — the ordered lifecycle rail. Filled steps are derived per matter from
  // its artifacts' stage labels.
  const LIFECYCLE_ORDER = ["received", "draft", "ai_redline", "legal_review", "sent", "counter", "signed"];

  const LIFECYCLE_LABELS = {
    received: "Received",
    draft: "Draft",
    ai_redline: "AI redline",
    legal_review: "Legal review",
    sent: "Sent",
    counter: "Counter",
    signed: "Signed",
  };

  const SOURCE_LABELS = {
    app: "In app",
    drive: "On file (Drive)",
    both: "In app + Drive",
  };

  // Rich, parallel-effort facets. Read defensively from payload.facets[key] (for
  // the option list + counts) and matter.facets[key] (for filtering). Absent today
  // on origin/main; the rail degrades gracefully when they are missing.
  const RICH_FACET_KEYS = ["governing_law", "non_solicit", "non_compete"];

  const RICH_FACET_LABELS = {
    governing_law: "Governing law",
    non_solicit: "Non-solicit",
    non_compete: "Non-compete",
  };

  function artifactStageLabel(artifact) {
    if (!artifact || typeof artifact !== "object") return "";
    if (artifact.stage_label) return String(artifact.stage_label);
    return ROLE_STAGE_LABELS[artifact.role] || String(artifact.role || "");
  }

  function lifecycleLabel(stage) {
    return LIFECYCLE_LABELS[stage] || String(stage || "");
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

  const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];

  // Sortable month key "YYYY-MM" from a matter's created_at. Undated matters fall
  // into a sentinel "0000-00" bucket that sorts last (oldest) under newest-first.
  function monthKey(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "0000-00";
    const month = String(date.getMonth() + 1).padStart(2, "0");
    return `${date.getFullYear()}-${month}`;
  }

  // Human label "June 2026" for a monthKey; the sentinel reads "Undated".
  function monthLabel(key) {
    if (!key || key === "0000-00") return "Undated";
    const [year, month] = String(key).split("-");
    const idx = Number(month) - 1;
    const name = MONTH_NAMES[idx];
    if (!name || !year) return "Undated";
    return `${name} ${year}`;
  }

  // Axis A — STATUS chip = the Repository board-column label. The payload field
  // `status` carries board_column; the dead `phase_label` is no longer surfaced.
  // Drive-only matters with no board_column -> "" (caller renders "—").
  function statusChip(matter) {
    const col = matter && matter.status;
    if (!col) return "";
    if (typeof RepositoryModel !== "undefined" && typeof RepositoryModel.boardColumnLabel === "function") {
      return RepositoryModel.boardColumnLabel(col);
    }
    return String(col);
  }

  // Axis B — ordered [{stage, filled}] for the lifecycle rail. A step is filled
  // when any artifact maps to that stage.
  function railSteps(matter) {
    const artifacts = matter && Array.isArray(matter.artifacts) ? matter.artifacts : [];
    const filled = new Set();
    artifacts.forEach((artifact) => {
      const stage = artifactStageLabel(artifact);
      if (stage) filled.add(stage);
    });
    return LIFECYCLE_ORDER.map((stage) => ({ stage, filled: filled.has(stage) }));
  }

  // Defensive read of a rich-facet value for a matter. Prefers matter.facets[key],
  // falls back to a top-level matter[key]; returns undefined when absent.
  function matterFacetValue(matter, key) {
    if (!matter || typeof matter !== "object") return undefined;
    const facets = matter.facets;
    if (facets && typeof facets === "object" && facets[key] != null && facets[key] !== "") {
      return facets[key];
    }
    if (matter[key] != null && matter[key] !== "") return matter[key];
    return undefined;
  }

  function artifactCountLabel(count) {
    const value = Number(count || 0);
    return `${value} ${value === 1 ? "file" : "files"}`;
  }

  return {
    LIFECYCLE_ORDER,
    RICH_FACET_KEYS,
    RICH_FACET_LABELS,
    ROLE_STAGE_LABELS,
    artifactCountLabel,
    artifactStageLabel,
    counterpartyName,
    formatDate,
    lifecycleLabel,
    matterFacetValue,
    matterTitle,
    monthKey,
    monthLabel,
    railSteps,
    sourceLabel,
    statusChip,
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
        drive_timeout: "Drive slow to respond — retry shortly",
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

  // --- search tokens ---------------------------------------------------------
  function renderSearchTokens(fieldNode, activeFacets, handlers) {
    if (!fieldNode) return;
    const input = fieldNode.querySelector(".corpus-search-input");
    // Remove any previously-rendered tokens (everything before the input).
    fieldNode.querySelectorAll(".corpus-token").forEach((token) => token.remove());
    const fragments = [];
    activeFacets.forEach((values, key) => {
      values.forEach((value) => {
        fragments.push(tokenMarkup(key, value));
      });
    });
    if (fragments.length && input) {
      input.insertAdjacentHTML("beforebegin", fragments.join(""));
    }
    fieldNode.querySelectorAll(".corpus-token-remove").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const key = button.dataset.facetKey;
        const value = button.dataset.facetValue;
        if (handlers && typeof handlers.removeFacet === "function") {
          handlers.removeFacet(key, value);
        }
      });
    });
  }

  function tokenMarkup(key, value) {
    const label = facetValueLabel(key, value);
    return `
      <span class="corpus-token" data-facet-key="${html(key)}" data-facet-value="${html(value)}">
        ${html(label)}
        <button class="corpus-token-remove" type="button" data-facet-key="${html(key)}" data-facet-value="${html(value)}" aria-label="Remove ${html(label)} filter">×</button>
      </span>
    `;
  }

  // --- facet rail ------------------------------------------------------------
  // Stage facet value set = the 5 board columns (FIX A: no phantom phases).
  function stageFacetDefs() {
    if (typeof RepositoryModel !== "undefined" && Array.isArray(RepositoryModel.BOARD_COLUMNS)) {
      return RepositoryModel.BOARD_COLUMNS.map((column) => ({ value: column.id, label: column.label }));
    }
    return [
      { value: "generated", label: "Generated" },
      { value: "manual_upload", label: "Upload" },
      { value: "gmail_demo", label: "Inbox" },
      { value: "in_review", label: "In Review" },
      { value: "reviewed", label: "Reviewed" },
      { value: "sent", label: "Sent" },
    ];
  }

  const SOURCE_FACET_DEFS = [
    { value: "app", label: "In app" },
    { value: "both", label: "In app + Drive" },
    { value: "drive", label: "On file (Drive)" },
  ];

  const FLAG_FACET_DEFS = [{ value: "duplicate", label: "Duplicate" }];

  function allMatters(payload) {
    const groups = Array.isArray(payload.groups) ? payload.groups : [];
    return groups.flatMap((group) => (Array.isArray(group.matters) ? group.matters : []));
  }

  function countBy(matters, predicate) {
    let n = 0;
    matters.forEach((matter) => {
      if (predicate(matter)) n += 1;
    });
    return n;
  }

  function renderFacetRail(railNode, payload, activeFacets, handlers) {
    if (!railNode) return;
    const matters = allMatters(payload);
    const payloadFacets = payload && typeof payload.facets === "object" && payload.facets ? payload.facets : {};
    const sections = [];

    // Stage (board column).
    sections.push(
      facetGroupMarkup("stage", "Stage", stageFacetDefs().map((def) => ({
        ...def,
        count: countBy(matters, (m) => m.status === def.value),
      })), activeFacets, { rich: false })
    );

    // Source.
    sections.push(
      facetGroupMarkup("source", "Source", SOURCE_FACET_DEFS.map((def) => ({
        ...def,
        count: countBy(matters, (m) => m.source === def.value),
      })), activeFacets, { rich: false })
    );

    // Flags.
    sections.push(
      facetGroupMarkup("flags", "Flags", FLAG_FACET_DEFS.map((def) => ({
        ...def,
        count: countBy(matters, (m) => Boolean(m.duplicate)),
      })), activeFacets, { rich: false })
    );

    // Rich facets — read-only consume payload.facets[key] when present; else
    // degrade (dimmed/disabled "available once indexed").
    CorpusModel.RICH_FACET_KEYS.forEach((key) => {
      const title = CorpusModel.RICH_FACET_LABELS[key] || key;
      const backendOptions = Array.isArray(payloadFacets[key]) ? payloadFacets[key] : null;
      const present = facetPresentInPayload(key, backendOptions, matters);
      if (!present) {
        sections.push(degradedFacetGroupMarkup(key, title));
        return;
      }
      const options = richFacetOptions(key, backendOptions, matters);
      sections.push(facetGroupMarkup(key, title, options, activeFacets, { rich: true }));
    });

    railNode.innerHTML = sections.join("");
    bindFacetRail(railNode, handlers);
  }

  function facetPresentInPayload(key, backendOptions, matters) {
    if (backendOptions && backendOptions.length) return true;
    // Even without payload.facets, light the group up if any matter carries the
    // field (so it works the moment the backend lands per-matter facets).
    return matters.some((matter) => CorpusModel.matterFacetValue(matter, key) !== undefined);
  }

  function richFacetOptions(key, backendOptions, matters) {
    // Prefer the backend's option list + counts when present (read-only).
    if (backendOptions && backendOptions.length) {
      return backendOptions.map((option) => {
        if (option && typeof option === "object") {
          const value = String(option.value != null ? option.value : option.id != null ? option.id : "");
          return {
            value,
            label: String(option.label || value),
            count: Number(option.count != null ? option.count : 0),
          };
        }
        const value = String(option);
        return { value, label: value, count: 0 };
      });
    }
    // Otherwise derive options + counts from per-matter facet values.
    const counts = new Map();
    matters.forEach((matter) => {
      const value = CorpusModel.matterFacetValue(matter, key);
      if (value === undefined) return;
      const str = String(value);
      counts.set(str, (counts.get(str) || 0) + 1);
    });
    return Array.from(counts.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([value, count]) => ({ value, label: value, count }));
  }

  function facetGroupMarkup(key, title, options, activeFacets, { rich }) {
    const active = activeFacets.get(key) || new Set();
    const optionMarkup = options
      .map((option) => {
        const pressed = active.has(option.value);
        return `
          <button class="corpus-facet-option" type="button"
                  data-facet-key="${html(key)}" data-facet-value="${html(option.value)}"
                  aria-pressed="${pressed ? "true" : "false"}">
            <span class="corpus-facet-label">${html(option.label)}</span>
            <span class="corpus-facet-count">${html(String(option.count || 0))}</span>
          </button>
        `;
      })
      .join("");
    return `
      <section class="corpus-facet-group${rich ? " corpus-facet-group--rich" : ""}" data-facet-key="${html(key)}">
        <h3 class="corpus-facet-title">${html(title)}</h3>
        <div class="corpus-facet-options">${optionMarkup}</div>
      </section>
    `;
  }

  function degradedFacetGroupMarkup(key, title) {
    return `
      <section class="corpus-facet-group corpus-facet-group--rich" data-facet-key="${html(key)}" data-degraded>
        <h3 class="corpus-facet-title">${html(title)}</h3>
        <div class="corpus-facet-options">
          <button class="corpus-facet-option" type="button" aria-pressed="false" disabled>
            <span class="corpus-facet-label">Available once indexed</span>
          </button>
        </div>
      </section>
    `;
  }

  function bindFacetRail(railNode, handlers) {
    railNode.querySelectorAll(".corpus-facet-option").forEach((button) => {
      if (button.disabled) return;
      const group = button.closest(".corpus-facet-group");
      if (group && group.hasAttribute("data-degraded")) return;
      button.addEventListener("click", () => {
        const key = button.dataset.facetKey;
        const value = button.dataset.facetValue;
        if (!key || value === undefined) return;
        if (handlers && typeof handlers.toggleFacet === "function") {
          handlers.toggleFacet(key, value);
        }
      });
    });
  }

  function facetValueLabel(key, value) {
    if (key === "stage") {
      const def = stageFacetDefs().find((d) => d.value === value);
      if (def) return def.label;
    }
    if (key === "source") {
      return CorpusModel.sourceLabel(value) || value;
    }
    if (key === "flags") {
      const def = FLAG_FACET_DEFS.find((d) => d.value === value);
      if (def) return def.label;
    }
    return String(value);
  }

  // --- lifecycle rail --------------------------------------------------------
  function renderLifecycleRail(matter) {
    const steps = CorpusModel.railSteps(matter);
    const inner = steps
      .map((step, index) => {
        const connector = index > 0 ? `<span class="corpus-rail-connector" aria-hidden="true"></span>` : "";
        const label = CorpusModel.lifecycleLabel(step.stage);
        return `${connector}<span class="corpus-rail-step ${step.filled ? "is-filled" : "is-empty"}" data-stage="${html(step.stage)}" title="${html(label)}"><span class="corpus-rail-dot" aria-hidden="true"></span></span>`;
      })
      .join("");
    return `<div class="corpus-lifecycle-rail" aria-label="Artifact lifecycle">${inner}</div>`;
  }

  // --- groups / matters ------------------------------------------------------
  // The Corpus has two grouping lenses, never blended:
  //   * "counterparty" (default) — backend payload.groups, one section per entity.
  //   * "month" — client-side regroup of the flat matter list into "Month YYYY"
  //     sections (newest first) so a recurring entity in the same month is
  //     scannable for duplicate-spotting. Each card keeps its counterparty sub.
  function renderGroups(listNode, payload, handlers, filterFn, groupBy) {
    if (!listNode) return 0;
    const predicate = typeof filterFn === "function" ? filterFn : () => true;
    if (groupBy === "month") {
      return renderMonthGroups(listNode, payload, handlers, predicate);
    }
    const groups = Array.isArray(payload.groups) ? payload.groups : [];
    let shown = 0;
    const groupMarkup = groups
      .map((group) => {
        const matters = (Array.isArray(group.matters) ? group.matters : []).filter(predicate);
        if (!matters.length) return "";
        shown += matters.length;
        return renderGroup(group, matters);
      })
      .filter(Boolean)
      .join("");
    listNode.innerHTML = groupMarkup;
    bindEvents(listNode, handlers);
    return shown;
  }

  function renderGroup(group, matters) {
    const matterCount = matters.length;
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

  function renderMonthGroups(listNode, payload, handlers, predicate) {
    const matters = allMatters(payload).filter(predicate);
    // Bucket by sortable month key.
    const buckets = new Map();
    matters.forEach((matter) => {
      const key = CorpusModel.monthKey(matter.created_at);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(matter);
    });
    // Newest month first (descending key); the "0000-00" undated sentinel lands last.
    const orderedKeys = Array.from(buckets.keys()).sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
    let shown = 0;
    const groupMarkup = orderedKeys
      .map((key) => {
        const bucket = buckets.get(key).slice().sort((a, b) =>
          String(b.created_at || "").localeCompare(String(a.created_at || ""))
        );
        if (!bucket.length) return "";
        shown += bucket.length;
        return renderMonthGroup(key, bucket);
      })
      .filter(Boolean)
      .join("");
    listNode.innerHTML = groupMarkup;
    bindEvents(listNode, handlers);
    return shown;
  }

  function renderMonthGroup(key, matters) {
    const matterCount = matters.length;
    // Count distinct counterparties that recur this month (>=2 matters) — the
    // duplicate-spotting hint. Cheap-counted off the bucket.
    const cpCounts = new Map();
    matters.forEach((matter) => {
      const name = CorpusModel.counterpartyName(matter);
      cpCounts.set(name, (cpCounts.get(name) || 0) + 1);
    });
    let recurring = 0;
    cpCounts.forEach((count) => {
      if (count > 1) recurring += 1;
    });
    const recurringNote = recurring
      ? `<span class="corpus-group-recurring" title="Counterparties with more than one NDA this month">${recurring} repeat ${recurring === 1 ? "entity" : "entities"}</span>`
      : "";
    return `
      <section class="corpus-group corpus-group--month">
        <header class="corpus-group-head">
          <h2 class="corpus-group-name">${html(CorpusModel.monthLabel(key))}</h2>
          <span class="corpus-group-meta">
            ${recurringNote}
            <span class="corpus-group-count">${matterCount} ${matterCount === 1 ? "matter" : "matters"}</span>
          </span>
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
    const status = CorpusModel.statusChip(matter);
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
            <span class="corpus-status-chip" title="Workflow status">${html(status || "—")}</span>
            ${matter.source ? `<span class="corpus-source-badge corpus-source-${html(matter.source)}" title="${html(CorpusModel.sourceLabel(matter.source))}">${html(CorpusModel.sourceLabel(matter.source) || matter.source)}</span>` : ""}
            ${matter.duplicate ? `<span class="corpus-duplicate-chip" title="More than one Drive folder maps to this matter">DUPLICATE</span>` : ""}
            <span class="corpus-artifact-count">${html(CorpusModel.artifactCountLabel(artifactCount))}</span>
          </span>
        </header>
        ${renderLifecycleRail(matter)}
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
    const stagePill = stage
      ? `<span class="corpus-artifact-stage" data-stage="${html(stage)}">${html(CorpusModel.lifecycleLabel(stage))}</span>`
      : "";
    return `
      <li class="corpus-artifact">
        <span class="corpus-artifact-seq" aria-hidden="true">${seq ? html(String(seq)) : "•"}</span>
        <span class="corpus-artifact-main">
          <span class="corpus-artifact-name">${html(artifact.filename || artifact.role || "Document")}</span>
          <span class="corpus-artifact-sub">
            ${stagePill}
            ${artifact.actor ? `<span class="corpus-artifact-actor">${html(artifact.actor)}</span>` : ""}
            ${artifact.version ? `<span class="corpus-artifact-version">v${html(String(artifact.version))}</span>` : ""}
            ${date ? `<span class="corpus-artifact-date">${html(date)}</span>` : ""}
          </span>
        </span>
        <span class="corpus-artifact-action">${action}</span>
      </li>
    `;
  }

  function renderSkeleton(listNode, count = 3) {
    if (!listNode) return;
    const cards = Array.from({ length: count })
      .map(
        () => `
        <section class="corpus-group corpus-skeleton" aria-hidden="true">
          <header class="corpus-group-head"><span class="corpus-skel-line corpus-skel-line--head"></span></header>
          <div class="corpus-matter-list">
            <div class="corpus-skel-row"><span class="corpus-skel-line"></span><span class="corpus-skel-line corpus-skel-line--short"></span></div>
            <div class="corpus-skel-row"><span class="corpus-skel-line"></span><span class="corpus-skel-line corpus-skel-line--short"></span></div>
          </div>
        </section>
      `
      )
      .join("");
    listNode.innerHTML = cards;
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

  return {
    renderDriveStatus,
    renderFacetRail,
    renderGroups,
    renderLifecycleRail,
    renderSearchTokens,
    renderSkeleton,
  };
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

  // Build a per-matter predicate from active facets + free-text query. AND across
  // facet keys, OR within a key; free text matches counterparty + title + filenames.
  function buildFilter(activeFacets, query) {
    const text = String(query || "").trim().toLowerCase();
    return (matter) => {
      if (text && !matterMatchesText(matter, text)) return false;
      for (const [key, values] of activeFacets.entries()) {
        if (!values || !values.size) continue;
        if (!matterMatchesFacet(matter, key, values)) return false;
      }
      return true;
    };
  }

  function matterMatchesText(matter, text) {
    const haystacks = [matter.counterparty, matter.title];
    if (Array.isArray(matter.artifacts)) {
      matter.artifacts.forEach((artifact) => haystacks.push(artifact && artifact.filename));
    }
    return haystacks.some((value) => String(value || "").toLowerCase().includes(text));
  }

  function matterMatchesFacet(matter, key, values) {
    if (key === "stage") return values.has(matter.status);
    if (key === "source") return values.has(matter.source);
    if (key === "flags") {
      // Currently only "duplicate".
      if (values.has("duplicate") && !matter.duplicate) return false;
      return true;
    }
    if (CorpusModel.RICH_FACET_KEYS.includes(key)) {
      const value = CorpusModel.matterFacetValue(matter, key);
      if (value === undefined) return false;
      return values.has(String(value));
    }
    return true;
  }

  function createController({
    panel,
    listNode,
    emptyNode,
    noResultsNode,
    statusNode,
    summaryNode,
    refreshButton,
    searchForm,
    searchInput,
    tokenField,
    searchClear,
    facetRail,
    groupToggle,
    openMatter,
  }) {
    let loadedOnce = false;
    let loading = false;
    let lastPayload = null;
    const state = { activeFacets: new Map(), query: "", groupBy: "counterparty" };

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
      if (noResultsNode) noResultsNode.hidden = true;
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

    const handlers = {
      openMatter,
      toggleFacet: (key, value) => {
        if (!state.activeFacets.has(key)) state.activeFacets.set(key, new Set());
        const set = state.activeFacets.get(key);
        if (set.has(value)) set.delete(value);
        else set.add(value);
        if (!set.size) state.activeFacets.delete(key);
        applyFilters();
      },
      removeFacet: (key, value) => {
        const set = state.activeFacets.get(key);
        if (!set) return;
        set.delete(value);
        if (!set.size) state.activeFacets.delete(key);
        applyFilters();
      },
    };

    function hasActiveFilters() {
      return Boolean(state.query.trim()) || state.activeFacets.size > 0;
    }

    function syncClearButton() {
      if (searchClear) searchClear.hidden = !hasActiveFilters();
    }

    function resetFilters() {
      state.query = "";
      state.activeFacets = new Map();
      if (searchInput) searchInput.value = "";
      applyFilters();
    }

    // Re-render groups + facet rail + tokens from the already-fetched payload.
    function applyFilters() {
      if (!lastPayload) return;
      CorpusRender.renderFacetRail(facetRail, lastPayload, state.activeFacets, handlers);
      CorpusRender.renderSearchTokens(tokenField, state.activeFacets, handlers);
      syncClearButton();

      const groups = Array.isArray(lastPayload.groups) ? lastPayload.groups : [];
      if (!groups.length) {
        renderEmptyState("No NDAs on file yet.");
        return;
      }
      clearEmptyState();
      const filter = buildFilter(state.activeFacets, state.query);
      const shown = CorpusRender.renderGroups(listNode, lastPayload, handlers, filter, state.groupBy);
      if (noResultsNode) noResultsNode.hidden = shown !== 0;
      if (listNode) listNode.hidden = shown === 0;
    }

    function syncGroupToggle() {
      if (!groupToggle) return;
      groupToggle.querySelectorAll("[data-corpus-group-by]").forEach((button) => {
        const pressed = button.dataset.corpusGroupBy === state.groupBy;
        button.setAttribute("aria-pressed", pressed ? "true" : "false");
      });
    }

    function setGroupBy(mode) {
      const next = mode === "month" ? "month" : "counterparty";
      if (next === state.groupBy) return;
      state.groupBy = next;
      syncGroupToggle();
      applyFilters();
    }

    function render(payload) {
      lastPayload = payload || {};
      CorpusRender.renderDriveStatus(statusNode, lastPayload.drive);
      renderSummary(lastPayload);
      applyFilters();
    }

    function load({ refresh = false } = {}) {
      if (loading) return Promise.resolve();
      setLoading(true);
      if (!loadedOnce) {
        renderEmptyState("Loading corpus…");
        clearEmptyState();
        CorpusRender.renderSkeleton(listNode, 3);
        if (statusNode) {
          statusNode.classList.remove("ok", "error");
          statusNode.classList.add("warn");
          statusNode.textContent = "Reconciling Drive — this can take a minute";
        }
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
            statusNode.classList.remove("ok", "warn");
            statusNode.classList.add("error");
            statusNode.textContent = "Corpus unavailable";
          }
        })
        .finally(() => setLoading(false));
    }

    if (refreshButton) {
      refreshButton.addEventListener("click", () => load({ refresh: true }));
    }
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        state.query = searchInput.value || "";
        applyFilters();
      });
    }
    if (searchForm) {
      searchForm.addEventListener("submit", (event) => event.preventDefault());
    }
    if (searchClear) {
      searchClear.addEventListener("click", () => resetFilters());
    }
    if (groupToggle) {
      groupToggle.querySelectorAll("[data-corpus-group-by]").forEach((button) => {
        button.addEventListener("click", () => setGroupBy(button.dataset.corpusGroupBy));
      });
      syncGroupToggle();
    }

    return {
      load,
      refresh: () => load({ refresh: true }),
      resetFilters,
      setGroupBy,
    };
  }

  return { buildFilter, createController, fetchCorpus };
})();

function createCorpusController(options) {
  return CorpusView.createController(options);
}

// Node test harness export (no-op in the browser). Lets the FE unit test import
// the pure helpers without a DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { CorpusModel, CorpusRender, CorpusView, createCorpusController };
}
