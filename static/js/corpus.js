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
  //
  // The master-filter set adds four SCALAR facets (mutuality / term_band /
  // review_outcome / origin) that ride the same single-scalar path as
  // governing_law, plus two MULTI-VALUE array facets (restraint_types /
  // clauses_present) handled by the MULTI_FACET_KEYS path below (ANY-match).
  const RICH_FACET_KEYS = [
    "governing_law",
    "non_solicit",
    "non_compete",
    "mutuality",
    "term_band",
    "review_outcome",
    "restraint_types",
    "clauses_present",
    "origin",
  ];

  const RICH_FACET_LABELS = {
    governing_law: "Governing law",
    non_solicit: "Non-solicit",
    non_compete: "Non-compete",
    mutuality: "Mutuality",
    term_band: "Term length",
    review_outcome: "Review outcome",
    restraint_types: "Restraint type",
    clauses_present: "Clauses present",
    origin: "Origin",
  };

  // Multi-value array facets: matter.facets[key] is an ARRAY of values. A matter
  // matches the facet if it carries ANY selected value (OR within the group). The
  // sidebar count for a value = matters whose array contains that value. These ride
  // a distinct read/match path from the single-scalar rich facets above.
  const MULTI_FACET_KEYS = ["restraint_types", "clauses_present"];

  function isMultiFacet(key) {
    return MULTI_FACET_KEYS.indexOf(key) !== -1;
  }

  // Per-facet value -> human label maps for the scalar master-filter facets. Values
  // absent from a map fall through to a humanised form of the raw value.
  const FACET_VALUE_LABELS = {
    mutuality: { mutual: "Mutual", one_way: "One-way" },
    term_band: { "<=2y": "2 years or less", "3-5y": "3–5 years", ">5y": "Over 5 years" },
    review_outcome: { clean: "Clean", needs_review: "Needs review", has_fail: "Has fail" },
    origin: { generated: "Generated", received: "Received" },
    restraint_types: {
      non_compete: "Non-compete",
      non_solicit: "Non-solicit",
      non_circumvention: "Non-circumvention",
    },
  };

  // Humanise an unmapped facet value: "needs_review" -> "Needs review".
  function humaniseFacetValue(value) {
    const str = String(value == null ? "" : value);
    if (!str) return str;
    const spaced = str.replace(/[_-]+/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }

  // Clause-presence facets: the backend emits the sentinel value "present" for a
  // matter whose review carried the clause (governing_law-style single scalar). A
  // bare "present" reads poorly in the rail/token, so these render "Present".
  const CLAUSE_PRESENCE_KEYS = ["non_solicit", "non_compete"];
  const CLAUSE_PRESENCE_VALUE_LABELS = { present: "Present" };

  function isClausePresenceFacet(key) {
    return CLAUSE_PRESENCE_KEYS.indexOf(key) !== -1;
  }

  // Human label for a rich-facet value. Clause-presence facets map "present" ->
  // "Present"; the master-filter facets use their own value-label map (else a
  // humanised fallback); every other facet labels by its own value (govlaw codes).
  function richFacetValueLabel(key, value) {
    if (isClausePresenceFacet(key)) {
      return CLAUSE_PRESENCE_VALUE_LABELS[String(value)] || String(value);
    }
    const map = FACET_VALUE_LABELS[key];
    if (map) {
      const mapped = map[String(value)];
      if (mapped) return mapped;
      return humaniseFacetValue(value);
    }
    return String(value);
  }

  // Defensive read of a multi-value facet's array for a matter. Prefers
  // matter.facets[key], falls back to top-level matter[key]; returns [] when absent
  // or not an array. Stringifies entries so the option/count/filter keys agree.
  function matterFacetValues(matter, key) {
    if (!matter || typeof matter !== "object") return [];
    let raw;
    const facets = matter.facets;
    if (facets && typeof facets === "object" && Array.isArray(facets[key])) {
      raw = facets[key];
    } else if (Array.isArray(matter[key])) {
      raw = matter[key];
    } else {
      return [];
    }
    return raw
      .filter((v) => v != null && v !== "")
      .map((v) => String(v));
  }

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

  // Duplicate-document signal: backend emits matter.duplicate_document =
  // {matched_matter_id, matched_title, similarity} | null. Read defensively;
  // returns the object (truthy) or null. Tolerates the field on matter.facets
  // too, mirroring matterFacetValue's two-place read.
  function duplicateDocument(matter) {
    if (!matter || typeof matter !== "object") return null;
    const direct = matter.duplicate_document;
    if (direct && typeof direct === "object" && direct.matched_matter_id) return direct;
    const facets = matter.facets;
    if (facets && typeof facets === "object") {
      const viaFacet = facets.duplicate_document;
      if (viaFacet && typeof viaFacet === "object" && viaFacet.matched_matter_id) return viaFacet;
    }
    return null;
  }

  // EXECUTED = a fully-signed agreement (the "library"). The backend resolves
  // facets.signed to true only for the fully_signed workflow status (see
  // corpus_index._signed_from_status); anything still in-flight is false/null.
  // The Corpus DEFAULTS to executed-only; the header toggle widens to all.
  // Read defensively (facets first, then a top-level matter.signed/executed),
  // and treat ONLY a strict boolean true as executed — null/undefined/false are
  // all "in-progress" so the default gate never lets an unsigned matter through.
  function isExecuted(matter) {
    if (!matter || typeof matter !== "object") return false;
    const facets = matter.facets;
    if (facets && typeof facets === "object") {
      if (facets.signed === true || facets.executed === true) return true;
      if (facets.signed === false) return false;
    }
    return matter.signed === true || matter.executed === true;
  }

  // Counterparty matches >=2 matters. Read defensively (top-level or facets).
  function isRepeatEntity(matter) {
    if (!matter || typeof matter !== "object") return false;
    if (matter.repeat_entity === true) return true;
    const facets = matter.facets;
    return Boolean(facets && typeof facets === "object" && facets.repeat_entity === true);
  }

  // "92% match" label from a similarity in [0,1]. Defensive against strings.
  function similarityLabel(similarity) {
    const value = Number(similarity);
    if (!Number.isFinite(value)) return "";
    return `${Math.round(value * 100)}% match`;
  }

  // Security: scheme-allowlist before any URL is interpolated into an href.
  // Drive/download URLs arrive from the payload (and ultimately from matter
  // metadata that can be operator/counterparty-influenced), so a hostile value
  // like "javascript:alert(1)" must never reach an href. Allow only http(s);
  // neutralise everything else to "" (the caller then renders no link).
  // Protocol-relative ("//evil") and scheme-relative paths are allowed only when
  // they are plainly relative (start with "/" but not "//"); anything carrying a
  // disallowed scheme is dropped.
  function safeHref(url) {
    if (url == null) return "";
    // Strip ALL ASCII control chars + whitespace (incl. embedded NUL/newline/tab)
    // before inspecting the scheme — browsers ignore these, so "java\tscript:"
    // collapses to "javascript:" and is then rejected rather than slipping past.
    const stripped = String(url).replace(/[\u0000-\u0020\u007f]+/g, "");
    if (!stripped) return "";
    // A scheme is present when "scheme:" precedes the first "/", "?" or "#".
    // If a colon appears there, the scheme must be exactly http: or https:.
    const schemeMatch = stripped.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
    if (schemeMatch) {
      const scheme = schemeMatch[1].toLowerCase();
      // Pass legit links through as the original trimmed url (verbatim); only the
      // scheme check runs on the control-stripped form.
      if (scheme === "http" || scheme === "https") return String(url).trim();
      return "";
    }
    // Protocol-relative ("//host") carries an implicit scheme we cannot vet -> drop.
    if (/^\/\//.test(stripped)) return "";
    // No scheme: a relative URL ("/x", "x", "#frag", "?q") is safe to pass through.
    return String(url).trim();
  }

  return {
    LIFECYCLE_ORDER,
    RICH_FACET_KEYS,
    RICH_FACET_LABELS,
    MULTI_FACET_KEYS,
    ROLE_STAGE_LABELS,
    artifactCountLabel,
    artifactStageLabel,
    counterpartyName,
    duplicateDocument,
    formatDate,
    isClausePresenceFacet,
    isExecuted,
    isMultiFacet,
    isRepeatEntity,
    lifecycleLabel,
    matterFacetValue,
    matterFacetValues,
    richFacetValueLabel,
    matterTitle,
    monthKey,
    monthLabel,
    railSteps,
    safeHref,
    similarityLabel,
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

  // Flag facets. Three honest, distinct duplicate-class signals:
  //   * "duplicate"          — the Drive folder-collision flag (matter.duplicate).
  //                            Labelled "Drive copy" so it can't be read as a
  //                            content duplicate.
  //   * "repeat_entity"      — counterparty has >=2 matters (matter.repeat_entity).
  //   * "duplicate_document" — content near-duplicate of another matter
  //                            (matter.duplicate_document non-null).
  const FLAG_FACET_DEFS = [
    { value: "duplicate", label: "Drive copy" },
    { value: "repeat_entity", label: "Repeat entity" },
    { value: "duplicate_document", label: "Duplicate document" },
  ];

  function allMatters(payload, baseFilter) {
    const groups = Array.isArray(payload.groups) ? payload.groups : [];
    const flat = groups.flatMap((group) => (Array.isArray(group.matters) ? group.matters : []));
    // The optional baseFilter is the executed-only gate (or () => true when the
    // toggle is widened). Scoping the facet counts to the SAME set the groups
    // render keeps "sidebar count == filtered-result count" parity in BOTH modes.
    return typeof baseFilter === "function" ? flat.filter(baseFilter) : flat;
  }

  function countBy(matters, predicate) {
    let n = 0;
    matters.forEach((matter) => {
      if (predicate(matter)) n += 1;
    });
    return n;
  }

  // Single source of truth for whether a matter matches a Flags-facet value.
  // Reused by the sidebar count (renderFacetRail) and the filter
  // (CorpusView.matterMatchesFacet) so count == filtered-result parity holds.
  function flagMatches(matter, value) {
    if (value === "duplicate") return Boolean(matter && matter.duplicate);
    if (value === "repeat_entity") return CorpusModel.isRepeatEntity(matter);
    if (value === "duplicate_document") return CorpusModel.duplicateDocument(matter) !== null;
    return false;
  }

  function renderFacetRail(railNode, payload, activeFacets, handlers, baseFilter) {
    if (!railNode) return;
    const matters = allMatters(payload, baseFilter);
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

    // Flags. Each def counts via the SAME predicate the filter uses, so the
    // sidebar count == the filtered-result count (count/filter parity).
    sections.push(
      facetGroupMarkup("flags", "Flags", FLAG_FACET_DEFS.map((def) => ({
        ...def,
        count: countBy(matters, (m) => flagMatches(m, def.value)),
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
    // field (so it works the moment the backend lands per-matter facets). Multi-
    // value facets are "present" when any matter's array is non-empty.
    if (CorpusModel.isMultiFacet(key)) {
      return matters.some((matter) => CorpusModel.matterFacetValues(matter, key).length > 0);
    }
    return matters.some((matter) => CorpusModel.matterFacetValue(matter, key) !== undefined);
  }

  function richFacetOptions(key, backendOptions, matters) {
    // Prefer the backend's option list + counts when present (read-only). This
    // path is shared by scalar and multi-value facets (the backend pre-counts).
    if (backendOptions && backendOptions.length) {
      return backendOptions.map((option) => {
        if (option && typeof option === "object") {
          const value = String(option.value != null ? option.value : option.id != null ? option.id : "");
          return {
            value,
            label: String(option.label || CorpusModel.richFacetValueLabel(key, value)),
            count: Number(option.count != null ? option.count : 0),
          };
        }
        const value = String(option);
        return { value, label: CorpusModel.richFacetValueLabel(key, value), count: 0 };
      });
    }
    // Otherwise derive options + counts from per-matter facet values.
    const counts = new Map();
    if (CorpusModel.isMultiFacet(key)) {
      // Multi-value: a matter contributes +1 to EACH distinct value it carries, so
      // the per-value count == matters-whose-array-contains-it. This is the same
      // membership the filter uses (CorpusView.matterMatchesFacet), so count ==
      // filtered-result parity holds for the OR-within-group semantics.
      matters.forEach((matter) => {
        const seen = new Set(CorpusModel.matterFacetValues(matter, key));
        seen.forEach((str) => counts.set(str, (counts.get(str) || 0) + 1));
      });
    } else {
      matters.forEach((matter) => {
        const value = CorpusModel.matterFacetValue(matter, key);
        if (value === undefined) return;
        const str = String(value);
        counts.set(str, (counts.get(str) || 0) + 1);
      });
    }
    return Array.from(counts.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([value, count]) => ({ value, label: CorpusModel.richFacetValueLabel(key, value), count }));
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
    // Honest empty state: the group only shows here when NO matter carries the
    // facet. The clause-presence facets say so plainly (they currently match zero
    // matters because the clauses are not in the active playbook) -- no "available
    // once indexed" promise of imminent data. The generic rich facet (governing
    // law) keeps the neutral "no data yet" wording.
    const emptyLabel = CorpusModel.isClausePresenceFacet(key)
      ? "No matters with this clause"
      : "No data yet";
    return `
      <section class="corpus-facet-group corpus-facet-group--rich" data-facet-key="${html(key)}" data-degraded>
        <h3 class="corpus-facet-title">${html(title)}</h3>
        <div class="corpus-facet-options">
          <button class="corpus-facet-option" type="button" aria-pressed="false" disabled>
            <span class="corpus-facet-label">${html(emptyLabel)}</span>
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
    if (CorpusModel.isClausePresenceFacet(key)) {
      // Token reads e.g. "Non-solicit: Present" rather than "...: present".
      return `${CorpusModel.RICH_FACET_LABELS[key] || key}: ${CorpusModel.richFacetValueLabel(key, value)}`;
    }
    // Master-filter + governing-law facets read "Group: Value" (e.g.
    // "Mutuality: Mutual", "Term length: 3–5 years") so a chip is self-describing.
    if (CorpusModel.RICH_FACET_KEYS.includes(key)) {
      return `${CorpusModel.RICH_FACET_LABELS[key] || key}: ${CorpusModel.richFacetValueLabel(key, value)}`;
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

  // Option 1 — passive group-header badge for a repeat-entity counterparty.
  // Shows "Repeat entity · N NDAs" (purple) when ANY matter in the group is
  // flagged repeat_entity. N is the count of matters rendered in this group.
  // No click — it is informational only.
  function repeatEntityBadge(matters) {
    const list = Array.isArray(matters) ? matters : [];
    if (!list.some((m) => CorpusModel.isRepeatEntity(m))) return "";
    const n = list.length;
    return `<span class="corpus-repeat-entity-badge" title="This counterparty has more than one NDA on file">Repeat entity · ${html(String(n))} ${n === 1 ? "NDA" : "NDAs"}</span>`;
  }

  function renderGroup(group, matters) {
    const matterCount = matters.length;
    return `
      <section class="corpus-group">
        <header class="corpus-group-head">
          <h2 class="corpus-group-name">${html(CorpusModel.counterpartyName(group))}</h2>
          ${repeatEntityBadge(matters)}
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

  // Option 2 — amber card chip for a content near-duplicate. Reads
  // matter.duplicate_document = {matched_matter_id, matched_title, similarity}.
  // Renders "Duplicate document · NN% match → {matched_title}" as a button that
  // jumps to (scrolls + expands) the matched matter card. It is a <button> so the
  // matter-head toggle handler ignores it (it skips clicks on a, button).
  function renderDuplicateDocumentChip(matter) {
    const dup = CorpusModel.duplicateDocument(matter);
    if (!dup) return "";
    const matchedId = String(dup.matched_matter_id || "");
    if (!matchedId) return "";
    const matchedTitle = String(dup.matched_title || "another matter");
    const sim = CorpusModel.similarityLabel(dup.similarity);
    const simPart = sim ? `${sim} ` : "";
    return `<button class="corpus-dupdoc-chip" type="button" data-corpus-dupdoc-target="${html(matchedId)}" title="Near-duplicate content of ${html(matchedTitle)} — jump to it">Duplicate document · ${html(simPart)}→ ${html(matchedTitle)}</button>`;
  }

  function renderMatter(matter) {
    const artifacts = Array.isArray(matter.artifacts) ? matter.artifacts : [];
    const artifactCount = Number(matter.artifact_count ?? artifacts.length);
    const key = matter.matter_id || matter.open_in_drive_url || CorpusModel.matterTitle(matter);
    const date = CorpusModel.formatDate(matter.created_at);
    const inApp = Boolean(matter.in_app);
    // safeHref drops any non-http(s) scheme (e.g. a "javascript:" payload) so a
    // hostile URL can never reach the Open-in-Drive href; "" suppresses the link.
    const driveUrl = CorpusModel.safeHref(matter.open_in_drive_url || "");
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
            ${matter.duplicate ? `<span class="corpus-duplicate-chip" title="More than one Drive folder maps to this matter">DRIVE COPY</span>` : ""}
            ${renderDuplicateDocumentChip(matter)}
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
    const rawUrls = Array.isArray(matter.duplicate_folder_urls) ? matter.duplicate_folder_urls : [];
    // Scheme-allowlist each URL; drop any that neutralise to "" so no link with a
    // hostile/empty href is rendered. Re-number after filtering for stable labels.
    const urls = rawUrls.map((url) => CorpusModel.safeHref(url)).filter((url) => url);
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
    // Per-file Download is intentionally removed from the Corpus: the in-app
    // download_url returns a broken/error page, not the file. Corpus files live
    // in Drive — direct the user there. When the artifact carries its own Drive
    // file link, surface "View in Drive"; otherwise show an inline note pointing
    // at the matter card's "Open in Drive" affordance.
    // Scheme-allowlist the Drive link; a non-http(s) value neutralises to "".
    const driveFile = CorpusModel.safeHref(artifact.drive_file_url || "");
    let action = "";
    if (driveFile) {
      action = `<a class="corpus-link" href="${html(driveFile)}" target="_blank" rel="noopener noreferrer">View in Drive</a>`;
    } else {
      action = `<span class="corpus-artifact-in-drive" title="Corpus files live in Drive — use Open in Drive above">In Drive</span>`;
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
    // Duplicate-document chip — jump to (scroll + expand) the matched matter card
    // within the corpus list. Pure in-page navigation, no refetch.
    listNode.querySelectorAll("[data-corpus-dupdoc-target]").forEach((chip) => {
      chip.addEventListener("click", (event) => {
        event.stopPropagation();
        event.preventDefault();
        const targetId = chip.dataset.corpusDupdocTarget;
        if (!targetId) return;
        jumpToMatter(listNode, targetId);
      });
    });
  }

  // Scroll the matched matter card into view and expand it. Reuses the existing
  // disclosure DOM shape so the jumped-to card opens exactly as a manual click.
  function jumpToMatter(listNode, matterId) {
    if (!listNode) return;
    const escaped =
      typeof CSS !== "undefined" && typeof CSS.escape === "function"
        ? CSS.escape(matterId)
        : String(matterId).replace(/"/g, '\\"');
    const card = listNode.querySelector(`[data-corpus-matter="${escaped}"]`);
    if (!card) return;
    const head = card.querySelector("[data-corpus-toggle]");
    const body = card.querySelector(".corpus-matter-body");
    if (head && body && head.getAttribute("aria-expanded") !== "true") {
      head.setAttribute("aria-expanded", "true");
      body.hidden = false;
      const disclosure = head.querySelector(".corpus-matter-disclosure");
      if (disclosure) disclosure.textContent = "▾";
    }
    if (typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    card.classList.add("corpus-matter--jump-target");
    setTimeout(() => card.classList.remove("corpus-matter--jump-target"), 1600);
  }

  return {
    flagMatches,
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

  // The fresh-user onboarding empty-state markup. A genuinely empty corpus (no
  // matters, no active filters) is a new-user state, so instead of a bare "No
  // NDAs" line we offer a welcoming "get started" panel: generate the first NDA,
  // connect Gmail to import inbound ones, and (when Drive is not yet connected) a
  // gentle Drive nudge. Pure + string-only so it is unit-testable; no user-
  // controlled values are interpolated, so there is nothing to escape here.
  function onboardingEmptyHtml(driveConnected = false) {
    const driveHint = driveConnected
      ? ""
      : `<p class="corpus-empty-drive-hint">Connect your Google Drive in Admin to archive signed NDAs and reconcile your full corpus.</p>`;
    return `
        <div class="corpus-empty-card" role="note" aria-label="Get started with your corpus">
          <h2 class="corpus-empty-title">No NDAs on file yet</h2>
          <p class="corpus-empty-lead">Your corpus fills up as you generate and sign NDAs. Here's how to get started.</p>
          <div class="corpus-empty-actions">
            <button class="corpus-empty-action" type="button" data-onboarding-goto="generator">Generate your first NDA</button>
            <button class="corpus-empty-action secondary" type="button" data-onboarding-goto="admin">Connect Gmail to import inbound NDAs</button>
          </div>
          ${driveHint}
        </div>
      `;
  }

  // The executed-only gate: a single predicate, defaulting ON, that admits only
  // fully-signed matters (the library). The Corpus toggle flips it off to widen
  // to ALL matters. Kept as its own one-liner so the default is a clean
  // one-predicate gate layered ahead of the facet/text filters.
  function executedGate(executedOnly) {
    if (!executedOnly) return () => true;
    return (matter) => CorpusModel.isExecuted(matter);
  }

  // Build a per-matter predicate from the executed gate + active facets + free
  // text. AND across everything: a matter must pass the executed gate, then every
  // active facet key (OR within a key), then the free-text match. Free text
  // matches counterparty + title + filenames. `executedOnly` defaults true so the
  // Corpus is the executed library unless the caller widens it.
  function buildFilter(activeFacets, query, executedOnly = true) {
    const text = String(query || "").trim().toLowerCase();
    const gate = executedGate(executedOnly);
    return (matter) => {
      if (!gate(matter)) return false;
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
      // OR within the Flags key: a matter matches if it satisfies ANY selected
      // flag value. Each value reuses CorpusRender.flagMatches so the filter and
      // the sidebar count agree (count == filtered-result parity).
      for (const value of values) {
        if (CorpusRender.flagMatches(matter, value)) return true;
      }
      return false;
    }
    if (CorpusModel.isMultiFacet(key)) {
      // Multi-value array facet (restraint_types / clauses_present): OR within the
      // group — match if the matter's array contains ANY selected value. This is
      // the SAME membership richFacetOptions counts, so count == filtered parity.
      const carried = CorpusModel.matterFacetValues(matter, key);
      if (!carried.length) return false;
      const set = new Set(carried);
      for (const value of values) {
        if (set.has(String(value))) return true;
      }
      return false;
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
    executedToggle,
    openMatter,
  }) {
    let loadedOnce = false;
    let loading = false;
    let lastPayload = null;
    // executedOnly defaults true: the Corpus opens as the executed (signed)
    // library; the header toggle flips it to include in-progress matters.
    const state = { activeFacets: new Map(), query: "", groupBy: "counterparty", executedOnly: true };
    let queryDebounceTimer = null;

    function setLoading(isLoading) {
      loading = isLoading;
      if (refreshButton) refreshButton.disabled = isLoading;
      if (panel) panel.classList.toggle("loading", isLoading);
    }

    function renderEmptyState(message) {
      if (!emptyNode) return;
      emptyNode.hidden = false;
      emptyNode.classList.remove("corpus-empty-onboarding");
      emptyNode.textContent = message;
      if (listNode) listNode.innerHTML = "";
      if (noResultsNode) noResultsNode.hidden = true;
    }

    // A fresh user with nothing on file sees a bare "No NDAs" line, which reads as
    // broken. Render a welcoming "get started" empty-state instead: generate the
    // first NDA, connect Gmail to import inbound ones, and (when Drive is not yet
    // connected) a gentle Drive nudge. Presentation only — all values escaped.
    function renderOnboardingEmptyState() {
      if (!emptyNode) return;
      emptyNode.hidden = false;
      emptyNode.classList.add("corpus-empty-onboarding");
      const driveConnected = Boolean(lastPayload && lastPayload.drive && lastPayload.drive.connected);
      emptyNode.innerHTML = onboardingEmptyHtml(driveConnected);
      if (listNode) listNode.innerHTML = "";
      if (noResultsNode) noResultsNode.hidden = true;
    }

    function clearEmptyState() {
      if (emptyNode) emptyNode.hidden = true;
    }

    // Summary counts the CURRENTLY-SHOWN set (executed-only by default), not the
    // backend grand total, so the header reads true for the active mode. When the
    // toggle widens to all, it falls back to the payload's own counts.
    function renderSummary(payload) {
      if (!summaryNode) return;
      let matters;
      let counterparties;
      if (state.executedOnly) {
        const groups = Array.isArray(payload.groups) ? payload.groups : [];
        const cps = new Set();
        matters = 0;
        groups.forEach((group) => {
          const list = (Array.isArray(group.matters) ? group.matters : []).filter((m) =>
            CorpusModel.isExecuted(m)
          );
          if (list.length) cps.add(CorpusModel.counterpartyName(group));
          matters += list.length;
        });
        counterparties = cps.size;
      } else {
        matters = Number(payload.matter_count || 0);
        counterparties = Number(payload.counterparty_count || 0);
      }
      const lens = state.executedOnly ? "Executed · " : "";
      summaryNode.textContent = `${lens}${counterparties} ${counterparties === 1 ? "counterparty" : "counterparties"} · ${matters} ${matters === 1 ? "matter" : "matters"}`;
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
    // The facet RAIL (counts) + facet TOKENS depend only on the matter set + the
    // active FACET selections — NOT on the free-text query — so a free-text
    // keystroke can pass { skipFacetRail: true } to re-render only the results
    // list/summary and skip the (expensive) rail recompute. Counts stay correct
    // because they never depended on the query.
    function applyFilters({ skipFacetRail = false } = {}) {
      if (!lastPayload) return;
      if (!skipFacetRail) {
        // The executed gate scopes the facet COUNTS to the shown set so sidebar
        // count == filtered-result count holds in both executed-only and all modes.
        const gate = executedGate(state.executedOnly);
        CorpusRender.renderFacetRail(facetRail, lastPayload, state.activeFacets, handlers, gate);
        CorpusRender.renderSearchTokens(tokenField, state.activeFacets, handlers);
      }
      syncClearButton();
      renderSummary(lastPayload);

      const groups = Array.isArray(lastPayload.groups) ? lastPayload.groups : [];
      if (!groups.length) {
        // A genuinely empty corpus with no active filters is a fresh-user state:
        // show the welcoming onboarding panel. With active filters it's a
        // filtered-to-nothing state, so keep the plain honest message.
        if (hasActiveFilters()) {
          renderEmptyState("No NDAs match your filters.");
        } else {
          renderOnboardingEmptyState();
        }
        return;
      }
      clearEmptyState();
      const filter = buildFilter(state.activeFacets, state.query, state.executedOnly);
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

    // The executed-only toggle. Two buttons: "Executed only" (default, pressed)
    // and "Include in-progress". aria-pressed mirrors state.executedOnly so the
    // active scope is announced.
    function syncExecutedToggle() {
      if (!executedToggle) return;
      executedToggle.querySelectorAll("[data-corpus-executed]").forEach((button) => {
        const wantsExecuted = button.dataset.corpusExecuted === "executed";
        const pressed = wantsExecuted === state.executedOnly;
        button.setAttribute("aria-pressed", pressed ? "true" : "false");
      });
    }

    function setExecutedOnly(executedOnly) {
      const next = Boolean(executedOnly);
      if (next === state.executedOnly) return;
      state.executedOnly = next;
      syncExecutedToggle();
      applyFilters();
    }

    function render(payload) {
      lastPayload = payload || {};
      CorpusRender.renderDriveStatus(statusNode, lastPayload.drive);
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
        // Free-text only changed: debounce + skip the facet-rail recompute (counts
        // are query-independent). Facet clicks / groupBy / refresh / reset still go
        // through applyFilters() with the rail rebuild intact.
        if (queryDebounceTimer) clearTimeout(queryDebounceTimer);
        queryDebounceTimer = setTimeout(() => {
          queryDebounceTimer = null;
          applyFilters({ skipFacetRail: true });
        }, 300);
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
    if (executedToggle) {
      executedToggle.querySelectorAll("[data-corpus-executed]").forEach((button) => {
        button.addEventListener("click", () =>
          setExecutedOnly(button.dataset.corpusExecuted === "executed")
        );
      });
      syncExecutedToggle();
    }

    return {
      load,
      refresh: () => load({ refresh: true }),
      resetFilters,
      setGroupBy,
      setExecutedOnly,
    };
  }

  return { buildFilter, createController, executedGate, fetchCorpus, onboardingEmptyHtml };
})();

function createCorpusController(options) {
  return CorpusView.createController(options);
}

// Node test harness export (no-op in the browser). Lets the FE unit test import
// the pure helpers without a DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { CorpusModel, CorpusRender, CorpusView, createCorpusController };
}
