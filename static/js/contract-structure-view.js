function createContractStructureController({ state, root }) {
  const romanNumberPattern = "[IVXLCDM]+";
  const baseIdentifierPartPattern = `(?:${romanNumberPattern}|[A-Za-z]|\\d+[A-Za-z]*)`;
  const parentheticalIdentifierPartPattern = "\\([A-Za-z0-9]+\\)";
  const identifierPartPattern = `(?:${baseIdentifierPartPattern}(?:${parentheticalIdentifierPartPattern})*|${parentheticalIdentifierPartPattern})`;
  const explicitNumberPattern = `${identifierPartPattern}(?:\\.${identifierPartPattern})*`;
  const numberedNumberPattern = `${identifierPartPattern}(?:\\.${identifierPartPattern})*`;
  const explicitHeadingRegex = new RegExp(`^(clause|article|section|schedule|annex|annexure|appendix)\\s+(${explicitNumberPattern})(\\s*[:.\\-\\u2013\\u2014]\\s*|\\s+)(.*)$`, "i");
  const numberedHeadingRegex = new RegExp(`^(${numberedNumberPattern})(?:\\s*[:.\\-\\u2013\\u2014]\\s*|\\s+)(.+)$`);
  const operativeSentenceRegex = /\b(?:shall|must|will|may|can|agrees?|undertakes?|covenants?|represents?|warrants?|is|are|was|were|has|have|means|includes?|excludes?|not|appl(?:y|ies)|surviv(?:e|es|ed|ing)|remain(?:s|ed|ing)?)\b/i;

  function render() {
    if (!root) return;
    // LOADING STATE: while a background AI review runs for the selected matter,
    // the Structure tab would otherwise show stale/empty content (the previous
    // structure map, or an empty placeholder). Paint a "Building structure map…"
    // shimmer skeleton instead, PAIRED with honest copy. The skeleton is generic
    // (a tile grid + a few rows) — it never previews the real section count. It is
    // replaced by the real map the moment the review completes and render() re-runs.
    // Guarded so a harness without MatterUtils is a no-op (falls through to normal).
    if (reviewInProgress()) {
      root.innerHTML = renderStructureSkeleton();
      return;
    }
    const structure = effectiveStructure();
    const allSections = Array.isArray(structure?.sections) ? structure.sections : [];
    // Honor the AI structure-validation demotion (structure_validation.py): a section
    // the validator flagged validation === "false_positive" is style-misuse noise (an
    // address line, signature field, bare "AND") whose aliases the backend already
    // stripped from the reference index. Drop it from the navigable list so it is not
    // shown as a genuine, clickable row or counted in the Sections tile. No-op when the
    // pass is off or on the client-side fallback structure (which never sets validation).
    const sections = allSections.filter((section) => String(section?.validation || "") !== "false_positive");
    const demotedCount = allSections.length - sections.length;
    const resolver = effectiveReferenceResolver(structure);
    const references = Array.isArray(resolver?.references) ? resolver.references : [];
    const resolverStats = resolver?.stats || {};
    const stats = structure?.stats || {};

    if (!state.latestReviewResult && !loadedParagraphs().length) {
      root.innerHTML = '<div class="structure-empty">Load or review an NDA to generate its structure map.</div>';
      return;
    }

    if (!structure || typeof structure !== "object") {
      root.innerHTML = '<div class="structure-empty">This review does not include a structure map yet. Reload the NDA or run the review again to generate it.</div>';
      return;
    }

    if (!sections.length) {
      root.innerHTML = '<div class="structure-empty">No section headings were detected in this review. The parser could not identify clauses, articles, numbered headings, or uppercase section labels.</div>';
      return;
    }

    const byId = sectionsById(sections);
    root.innerHTML = `
      ${renderReferenceFlags(references)}

      <div class="structure-summary" aria-label="Structure map summary">
        ${summaryTile("Sections", demotedCount > 0 ? sections.length : (stats.section_count ?? sections.length))}
        ${summaryTile("Mapped paragraphs", stats.mapped_paragraph_count ?? mappedParagraphCount(sections))}
        ${summaryTile("References", resolverStats.reference_count ?? references.length)}
        ${summaryTile("Resolved", resolverStats.resolved_reference_count ?? resolvedReferenceCount(references))}
        ${summaryTile("Unmapped paragraphs", stats.unmapped_paragraph_count ?? 0)}
        ${summaryTile("Source-backed", demotedCount > 0 ? sourceBackedSectionCount(sections) : (stats.source_backed_section_count ?? sourceBackedSectionCount(sections)))}
        ${summaryTile("Word numbers", stats.docx_numbered_paragraph_count ?? 0)}
        ${summaryTile("Tables", stats.table_paragraph_count ?? 0)}
      </div>

      <section class="structure-section-list structure-section-tree" aria-label="Detected contract sections">
        ${renderSectionTree(sections, byId)}
      </section>

      ${renderReferences(references, byId)}
    `;
    bindStructureRowKeyboard();
  }

  // True while a background AI review is running for the selected matter. Read via
  // the shared MatterUtils predicate (review_status === "in_progress") so the
  // Structure tab's loading state tracks the SAME signal the board badge + review
  // header use. Resolved lazily off window so an isolated test/load order without
  // the bridge degrades to "not in progress" (the normal render path) rather than
  // throwing a ReferenceError.
  function reviewInProgress() {
    const utils = typeof window !== "undefined" ? window.MatterUtils : undefined;
    if (!utils || typeof utils.reviewInProgress !== "function") return false;
    return Boolean(utils.reviewInProgress(state.selectedMatter));
  }

  // The "Building structure map…" shimmer skeleton: a generic summary-tile grid
  // plus a few section-row placeholders, headed by honest in-progress copy. The
  // shapes are neutral (never the real section/tile counts). The shimmer animation
  // itself is gated behind prefers-reduced-motion in CSS (.skeleton-block).
  function renderStructureSkeleton() {
    const tile = () => '<div class="skeleton-block structure-skeleton-tile"></div>';
    const tiles = new Array(8).fill(0).map(tile).join("");
    const rows = new Array(4).fill(0).map(() => '<div class="skeleton-block"></div>').join("");
    return `
      <div class="structure-skeleton" role="status" aria-live="polite">
        <div class="review-skeleton-copy">
          <span class="skeleton-dot" aria-hidden="true"></span>
          <span>Building structure map… this runs with the review.</span>
        </div>
        <div class="structure-skeleton-summary" aria-hidden="true">${tiles}</div>
        <div class="structure-skeleton-rows" aria-hidden="true">${rows}</div>
      </div>`;
  }

  // The global delegated [data-para-ref] handler (app.js) covers row CLICKS for free,
  // but it is click-only. Keep the keyboard-accessible rows usable by translating
  // Enter/Space on a focused row into the same jumpToParagraph the click path uses.
  // Rebound per render because render() replaces root.innerHTML wholesale.
  function bindStructureRowKeyboard() {
    if (!root) return;
    // Cover every keyboard-focusable jump target the tab renders: the section rows
    // (.structure-row-nav) AND the cross-reference source/target links, all of which
    // carry data-para-ref + role="button". The global click handler already fires for
    // their clicks; this only adds the matching Enter/Space keyboard activation.
    root.querySelectorAll('[data-para-ref][role="button"]').forEach((row) => {
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
        event.preventDefault();
        const paragraphId = row.getAttribute("data-para-ref");
        if (paragraphId && typeof jumpToParagraph === "function") jumpToParagraph(paragraphId);
      });
    });
  }

  // VIEW 1 -- dangling-reference red flags. The #1 human-insight win: the backend
  // already computes which cross-references point at sections that do not exist
  // ("Clause 9 references Schedule 3, which doesn't exist") and discards the signal
  // into a field nothing rendered. Surface it as a coloured callout at the very top.
  //
  // Primary source: state.latestReviewResult.reference_integrity, the document-level
  // roll-up built by reference_resolver.build_reference_integrity_signal and attached
  // in ai_first_review.py. It is GUARDED (DOCX-with-numbering only, collapse detector)
  // so it only fires when the cross-reference map is trustworthy -- exactly when we
  // want a red flag and never the cry-wolf case.
  //   - applicable && status === "issues_found" -> issues[].summary as RED callouts
  //     (a genuine drafting defect: a referenced section is missing) and
  //     ambiguous_issues[].summary as AMBER callouts (target unknown, not a defect).
  //
  // Fallback: when reference_integrity is absent (deterministic-only / PDF, or until
  // the engine fix lands), derive danglers from the resolver itself --
  // references[].status === "unresolved" -- so the insight still shows. The fallback
  // is intentionally conservative (only fully-unresolved references, never partials)
  // to mirror the guarded backend signal and avoid false alarms on collapsed parses.
  function renderReferenceFlags(references) {
    const integrity = state.latestReviewResult?.reference_integrity;
    let redSummaries = [];
    let amberSummaries = [];

    if (integrity && typeof integrity === "object" && integrity.applicable) {
      if (String(integrity.status || "") === "issues_found") {
        redSummaries = collectSummaries(integrity.issues);
      }
      amberSummaries = collectSummaries(integrity.ambiguous_issues);
    } else if (!integrity || typeof integrity !== "object" || !integrity.applicable) {
      // No trustworthy backend signal -- fall back to the resolver's own danglers.
      redSummaries = danglingSummariesFromReferences(references);
    }

    if (!redSummaries.length && !amberSummaries.length) return "";

    const redBlock = redSummaries.length
      ? `
        <div class="structure-flag structure-flag-danger" role="alert">
          <strong>${escapeHtml(redSummaries.length === 1 ? "Dangling reference" : `${redSummaries.length} dangling references`)}</strong>
          <ul>
            ${redSummaries.map((summary) => `<li>${escapeHtml(summary)}</li>`).join("")}
          </ul>
        </div>
      `
      : "";
    const amberBlock = amberSummaries.length
      ? `
        <div class="structure-flag structure-flag-warn" role="status">
          <strong>${escapeHtml(amberSummaries.length === 1 ? "Ambiguous reference" : `${amberSummaries.length} ambiguous references`)}</strong>
          <ul>
            ${amberSummaries.map((summary) => `<li>${escapeHtml(summary)}</li>`).join("")}
          </ul>
        </div>
      `
      : "";
    return `
      <section class="structure-flags" aria-label="Reference integrity flags">
        ${redBlock}
        ${amberBlock}
      </section>
    `;
  }

  function collectSummaries(issues) {
    if (!Array.isArray(issues)) return [];
    return issues
      .map((issue) => String(issue?.summary || "").trim())
      .filter(Boolean);
  }

  // Fallback danglers from the raw resolver when the guarded integrity signal is
  // unavailable. Only fully-unresolved references count; we phrase a human summary
  // mirroring the backend ("X references Y, which doesn't exist in this document").
  function danglingSummariesFromReferences(references) {
    if (!Array.isArray(references)) return [];
    const summaries = [];
    references.forEach((reference) => {
      if (String(reference?.status || "") !== "unresolved") return;
      const referenceText = String(reference?.reference_text || "").trim();
      const missing = Array.isArray(reference?.unresolved_numbers)
        ? reference.unresolved_numbers.map((value) => String(value).trim()).filter(Boolean)
        : [];
      const subject = referenceText || "A cross-reference";
      const targetPhrase = missing.length
        ? `${kindLabel(reference?.kind) || "section"} ${missing.join(", ")}`
        : "a section";
      summaries.push(`${subject} points to ${targetPhrase}, which doesn't exist in this document.`);
    });
    return summaries;
  }

  function effectiveStructure() {
    const existing = state.latestReviewResult?.contract_structure;
    if (existing && typeof existing === "object") return existing;

    const paragraphs = loadedParagraphs();
    if (!paragraphs.length) return existing;

    const fallback = buildStructureFromParagraphs(paragraphs);
    if (state.latestReviewResult && !state.latestReviewResult.contract_structure) {
      state.latestReviewResult.contract_structure = fallback;
    }
    return fallback;
  }

  function effectiveReferenceResolver(structure) {
    const existing = state.latestReviewResult?.reference_resolver;
    if (existing && typeof existing === "object") return existing;

    const paragraphs = loadedParagraphs();
    if (!structure || !paragraphs.length) return existing;

    const fallback = resolveReferencesFromParagraphs(paragraphs, structure);
    if (state.latestReviewResult && !state.latestReviewResult.reference_resolver) {
      state.latestReviewResult.reference_resolver = fallback;
    }
    return fallback;
  }

  function loadedParagraphs() {
    if (Array.isArray(state.reviewParagraphs) && state.reviewParagraphs.length) return state.reviewParagraphs;
    if (Array.isArray(state.latestReviewResult?.paragraphs)) return state.latestReviewResult.paragraphs;
    return [];
  }

  // Build a section-id -> section lookup so a child row can show its parent's
  // human heading/label instead of the raw `section-N` id.
  function sectionsById(sections) {
    const byId = {};
    sections.forEach((section) => {
      const id = section?.id ? String(section.id) : "";
      if (id) byId[id] = section;
    });
    return byId;
  }

  // Human label for a section, preferring its numbered label ("Clause 4"), then its
  // heading text, then a generic fallback. Never the raw `section-N` id.
  function sectionDisplayName(section) {
    if (!section || typeof section !== "object") return "";
    return section.label || section.heading || section.heading_text || "";
  }

  // VIEW 2 -- visual outline tree. The old view printed a FLAT list and put the
  // hierarchy in a tiny "Parent: Clause 4" footnote. Build a real nested tree from
  // each section's parent_id so sub-clauses nest under their parent and whole
  // subtrees collapse, the way a reader actually thinks about a contract's outline.
  //
  // Orphan-rooting: a section whose parent_id is null OR points at an id that is not
  // in the (post-demotion) section list becomes a root. This is what keeps the tree
  // honest when the AI structure-validation pass demoted a parent out of the list, or
  // when the parser produced a dangling parent_id -- we never drop the child, we just
  // promote it to the top level rather than orphan it into nothing.
  function renderSectionTree(sections, byId) {
    const roots = buildSectionForest(sections, byId);
    if (!roots.length) return "";
    return `<ul class="structure-tree-list" role="tree">${roots.map((node) => renderTreeNode(node, byId)).join("")}</ul>`;
  }

  // Group sections into a parent->children forest, preserving document order at each
  // level. Cycle-safe: a section can never be its own ancestor (a child whose chain
  // loops back is re-rooted), so a malformed parent_id graph can't infinite-loop.
  function buildSectionForest(sections, byId) {
    const childrenByParent = new Map();
    const roots = [];
    sections.forEach((section) => {
      const parentId = section?.parent_id ? String(section.parent_id) : "";
      const hasRealParent = parentId && byId[parentId] && !isAncestorCycle(section, byId);
      if (hasRealParent) {
        if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
        childrenByParent.get(parentId).push(section);
      } else {
        roots.push(section);
      }
    });
    const attach = (section) => ({
      section,
      children: (childrenByParent.get(String(section.id || "")) || []).map(attach),
    });
    return roots.map(attach);
  }

  // True when following parent_id from `section` revisits `section` (a cycle) -- such a
  // node must be re-rooted, not nested, to keep the forest acyclic.
  function isAncestorCycle(section, byId) {
    const startId = String(section?.id || "");
    const seen = new Set([startId]);
    let current = section?.parent_id ? byId[String(section.parent_id)] : null;
    while (current) {
      const currentId = String(current.id || "");
      if (seen.has(currentId)) return true;
      seen.add(currentId);
      current = current.parent_id ? byId[String(current.parent_id)] : null;
    }
    return false;
  }

  function renderTreeNode(node, byId) {
    const section = node.section;
    const rowHtml = renderSectionRow(section, byId);
    if (!node.children.length) {
      return `<li class="structure-tree-node" role="treeitem">${rowHtml}</li>`;
    }
    // A node with children is a collapsible <details> (open by default) so a reviewer
    // can fold a whole clause's sub-tree away. The summary holds the section row; the
    // nested <ul> holds the children, recursively.
    return `
      <li class="structure-tree-node structure-tree-branch" role="treeitem">
        <details class="structure-tree-details" open>
          <summary class="structure-tree-summary">${rowHtml}</summary>
          <ul class="structure-tree-list" role="group">
            ${node.children.map((child) => renderTreeNode(child, byId)).join("")}
          </ul>
        </details>
      </li>
    `;
  }

  function renderSectionRow(section, byId) {
    const level = Math.max(0, Math.min(5, Number(section.level || 0)));
    const indent = 14 + (level * 16);
    const paragraphs = paragraphRangeLabel(section);
    // Show the parent's human heading ("Parent: Clause 4"), looked up by parent_id.
    // If the parent section isn't in the list (or has no human name), omit the row
    // rather than print the raw `section-N` id to the reviewer.
    const parentSection = section.parent_id ? (byId || {})[String(section.parent_id)] : null;
    const parentName = sectionDisplayName(parentSection);
    const parent = parentName ? `<span>Parent: ${escapeHtml(parentName)}</span>` : "";
    const source = sourceSummary(section.source);
    // Clicking a row jumps the document viewer to the section's first paragraph.
    // data-para-ref is caught by the global delegated [data-para-ref] click handler
    // (app.js, capture phase), which dynamically-rendered rows fire through too;
    // role/tabindex + a local keydown handler keep the row keyboard-accessible.
    //
    // Source-backed gate (accuracy-or-nothing): the parser invents phantom sections on
    // flat/PDF docs (an address line or a table-cell digit scraped as "Clause 145"),
    // which carry no `source`. Making such a row a live jump target would send the
    // reader to e.g. "1 Sheldon Square", so a row is only navigable when its section is
    // source-backed; an inferred section renders as a plain (non-clickable) row.
    const sourceBacked = section.source && typeof section.source === "object"
      && Object.keys(section.source).length > 0;
    const startParagraphId = sourceBacked && section.start_paragraph_id ? String(section.start_paragraph_id) : "";
    const sectionLabel = section.label || section.heading || "Section";
    const navAttrs = startParagraphId
      ? ` data-para-ref="${escapeHtml(startParagraphId)}" role="button" tabindex="0"`
      + ` aria-label="${escapeHtml(`Jump to ${sectionLabel}`)}"`
      : "";
    const navClass = startParagraphId ? " structure-row-nav" : "";
    // Dim a node the parser only GUESSED at: a non-source-backed section, or one the
    // parser tagged low/medium confidence. The dim class is the honest visual cue that
    // separates a real heading (high confidence, source-backed, clickable) from the
    // parser's best guess (faint, not clickable) -- the tree should never present a
    // guess with the same authority as a real heading.
    const isGuess = !sourceBacked || !isHighConfidence(section.confidence);
    const dimClass = isGuess ? " structure-row-dim" : "";
    return `
      <article class="structure-row${navClass}${dimClass}" style="--structure-indent: ${indent}px"${navAttrs}>
        <span class="structure-level-marker" aria-hidden="true"></span>
        <div class="structure-row-main">
          <div class="structure-row-title">
            <strong>${escapeHtml(sectionLabel)}</strong>
            <span>${escapeHtml(kindLabel(section.kind))}</span>
          </div>
          <p>${escapeHtml(section.heading || section.heading_text || section.label || "Untitled section")}</p>
          <small>
            <span>${escapeHtml(paragraphs)}</span>
            <span>${escapeHtml(confidenceLabel(section.confidence))}</span>
            ${source ? `<span>${escapeHtml(source)}</span>` : ""}
            ${isGuess ? `<span class="structure-row-guess">Parser's guess</span>` : ""}
            ${parent}
          </small>
        </div>
      </article>
    `;
  }

  function isHighConfidence(confidence) {
    return String(confidence || "").toLowerCase() === "high";
  }

  // VIEW 3 -- clickable cross-reference links, grouped by their source section. The
  // old view was a flat 12-row dump of reference_text + a comma-joined target string,
  // none of it clickable and capped at 12 (so later references silently vanished).
  //
  // Now every reference renders as "From [source section] -> [target label]", BOTH
  // ends clickable, jumping the document viewer via the same delegated [data-para-ref]
  // handler the section rows use. Grouped by source section so a reviewer reads "here
  // is everything Clause 9 points at" in one place; no row cap, so nothing is hidden.
  //
  // Jump targets: the SOURCE side jumps to the source section's first paragraph
  // (byId[source_section_id].start_paragraph_id). The TARGET side jumps to the target
  // section's first paragraph -- the resolver target record carries paragraph_ids (its
  // ordered paragraph list) but not start_paragraph_id, so we use paragraph_ids[0].
  // A side is only made clickable when it resolves to a real paragraph id (same
  // accuracy-or-nothing gate as the section rows); otherwise it renders as plain text.
  function renderReferences(references, byId) {
    if (!Array.isArray(references) || !references.length) return "";
    const groups = groupReferencesBySource(references, byId);
    if (!groups.length) return "";
    return `
      <section class="structure-references" aria-label="Cross-references">
        <h2>Cross-references</h2>
        ${groups.map((group) => renderReferenceGroup(group, byId)).join("")}
      </section>
    `;
  }

  function groupReferencesBySource(references, byId) {
    const order = [];
    const groupsBySource = new Map();
    references.forEach((reference) => {
      const sourceId = reference?.source_section_id ? String(reference.source_section_id) : "";
      const key = sourceId || "__document__";
      if (!groupsBySource.has(key)) {
        groupsBySource.set(key, { key, sourceId, references: [] });
        order.push(key);
      }
      groupsBySource.get(key).references.push(reference);
    });
    return order.map((key) => groupsBySource.get(key));
  }

  function renderReferenceGroup(group, byId) {
    const sourceSection = group.sourceId ? (byId || {})[group.sourceId] : null;
    const sourceLabel = sectionDisplayName(sourceSection) || "Document body";
    const sourceJumpId = jumpTargetForSection(sourceSection);
    const sourceHeading = sourceJumpId
      ? `<span class="structure-xref-from" ${paraRefAttrs(sourceJumpId, `Jump to ${sourceLabel}`)}>${escapeHtml(sourceLabel)}</span>`
      : `<span class="structure-xref-from structure-xref-plain">${escapeHtml(sourceLabel)}</span>`;
    return `
      <article class="structure-xref-group">
        <div class="structure-xref-source">From ${sourceHeading}</div>
        <ul class="structure-xref-links">
          ${group.references.map((reference) => renderReferenceLink(reference, byId)).join("")}
        </ul>
      </article>
    `;
  }

  function renderReferenceLink(reference, byId) {
    const referenceText = String(reference?.reference_text || "Reference").trim() || "Reference";
    const status = referenceStatusLabel(reference?.status);
    const targets = Array.isArray(reference?.targets) ? reference.targets : [];
    let targetHtml;
    if (targets.length) {
      targetHtml = targets.map((target) => {
        const label = String(target?.label || target?.heading || "").trim()
          || sectionDisplayName(target) || "Section";
        const jumpId = jumpTargetForSection(target);
        return jumpId
          ? `<span class="structure-xref-to" ${paraRefAttrs(jumpId, `Jump to ${label}`)}>${escapeHtml(label)}</span>`
          : `<span class="structure-xref-to structure-xref-plain">${escapeHtml(label)}</span>`;
      }).join('<span class="structure-xref-sep">, </span>');
    } else {
      const missing = Array.isArray(reference?.unresolved_numbers) && reference.unresolved_numbers.length
        ? `Unresolved ${reference.unresolved_numbers.join(", ")}`
        : "No target";
      targetHtml = `<span class="structure-xref-to structure-xref-plain structure-xref-missing">${escapeHtml(missing)}</span>`;
    }
    return `
      <li class="structure-xref-link">
        <span class="structure-xref-text">${escapeHtml(referenceText)}</span>
        <span class="structure-xref-arrow" aria-hidden="true">&rarr;</span>
        ${targetHtml}
        <small class="structure-xref-status">${escapeHtml(status)}</small>
      </li>
    `;
  }

  // Resolve a section (a structure section OR a resolver target record) to the
  // paragraph id a jump should land on. Structure sections carry start_paragraph_id;
  // resolver target records carry an ordered paragraph_ids list -- prefer the explicit
  // start, fall back to the first paragraph id. Returns "" when neither exists, which
  // makes the link render as plain (non-clickable) text.
  function jumpTargetForSection(section) {
    if (!section || typeof section !== "object") return "";
    if (section.start_paragraph_id) return String(section.start_paragraph_id);
    if (Array.isArray(section.paragraph_ids) && section.paragraph_ids.length) {
      return String(section.paragraph_ids[0]);
    }
    return "";
  }

  function paraRefAttrs(paragraphId, ariaLabel) {
    return `data-para-ref="${escapeHtml(paragraphId)}" role="button" tabindex="0" aria-label="${escapeHtml(ariaLabel)}"`;
  }

  function summaryTile(label, value) {
    return `
      <div class="structure-summary-tile">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>
    `;
  }

  function mappedParagraphCount(sections) {
    const ids = new Set();
    sections.forEach((section) => {
      (section.paragraph_ids || []).forEach((paragraphId) => ids.add(String(paragraphId)));
    });
    return ids.size;
  }

  function resolvedReferenceCount(references) {
    return references.filter((reference) => reference.status === "resolved").length;
  }

  function sourceBackedSectionCount(sections) {
    return sections.filter((section) => section.source && typeof section.source === "object").length;
  }

  // Plain-English maps for the parser-internal source tokens. The backend emits
  // raw enums (source_part = header|footer|footnotes|...; source_kind =
  // paragraph|table_cell|docx_heading|...) that mean nothing to a reviewer. We
  // surface the human phrase, and humanize (never echo a raw `Source <token>`)
  // for any token the map does not cover.
  const SOURCE_PART_LABELS = {
    header: "From header",
    footer: "From footer",
    footnotes: "From footnotes",
    endnotes: "From endnotes",
    pdf: "From PDF text",
  };
  const SOURCE_KIND_LABELS = {
    paragraph: "Main body",
    table_cell: "In a table",
    supplemental: "Supplemental text",
    docx: "Word document",
    pdf: "PDF text",
    docx_heading: "Word heading",
    pdf_text: "PDF text",
  };

  function sourceSummary(source) {
    if (!source || typeof source !== "object") return "";
    if (source.numbering?.label) return `Word number ${source.numbering.label}`;
    // Deliberately do NOT surface `source.style_name` (e.g. the parser-internal token
    // "Heading2"). It is a Word style id meaningless to a reviewer; dropping it (rather
    // than printing "Style Heading2") is the friendly behaviour. A genuinely useful
    // source signal (Word numbering, table coordinates, source part) still renders below.
    if (source.table) {
      const table = source.table;
      return `Table ${table.table_index || "?"}, row ${table.row_index || "?"}, cell ${table.cell_index || "?"}`;
    }
    if (source.source_part) {
      const key = String(source.source_part).toLowerCase();
      return SOURCE_PART_LABELS[key] || humanizeStructureToken(source.source_part);
    }
    if (source.source_kind) {
      const key = String(source.source_kind).toLowerCase();
      return SOURCE_KIND_LABELS[key] || humanizeStructureToken(source.source_kind);
    }
    return "";
  }

  // Generic snake/kebab token -> Title Case fallback. Prefers the shared
  // window.humanizeId when present, so unknown enums read like English ("In a
  // table") instead of leaking the raw token ("Source table_cell").
  function humanizeStructureToken(token) {
    const generic = (typeof window !== "undefined" && typeof window.humanizeId === "function")
      ? window.humanizeId
      : null;
    if (generic) return generic(token);
    return String(token || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase())
      .trim();
  }

  function paragraphRangeLabel(section) {
    // Only a real numeric paragraph INDEX is a human-meaningful position ("Paragraph 47").
    // The *_paragraph_id fields are opaque internal ids (e.g. "p-3f2a", "¶<id>") that mean
    // nothing to a reviewer, so we never fall back to them — if there's no index, we say so.
    const start = humanParagraphPosition(section.start_index);
    const end = humanParagraphPosition(section.end_index);
    if (start === null && end === null) return "No paragraph range";
    if (start === null) return "No paragraph range";
    if (end === null || start === end) return `Paragraph ${start}`;
    return `Paragraphs ${start}-${end}`;
  }

  // A paragraph position is human-meaningful only when it is a real integer index.
  function humanParagraphPosition(value) {
    return Number.isInteger(value) ? value : null;
  }

  function kindLabel(kind) {
    const labels = {
      annex: "Annex",
      annexure: "Annexure",
      appendix: "Appendix",
      article: "Article",
      clause: "Clause",
      heading: "Heading",
      numbered: "Numbered",
      preamble: "Preamble",
      schedule: "Schedule",
      section: "Section",
    };
    if (labels[kind]) return labels[kind];
    // A novel/unknown kind (a raw snake_case backend token) is humanized rather
    // than flattened to "Section" or leaked verbatim — `cover_page` -> "Cover Page".
    // Falls back to "Section" only when there is no kind at all.
    const humanizer = typeof window !== "undefined" && typeof window.humanizeId === "function"
      ? window.humanizeId
      : null;
    const humanized = humanizer ? humanizer(kind) : "";
    return humanized || "Section";
  }

  function confidenceLabel(confidence) {
    const labels = {
      high: "High confidence",
      medium: "Medium confidence",
      low: "Low confidence",
    };
    return labels[String(confidence || "").toLowerCase()] || "Confidence unknown";
  }

  // Friendly label for a cross-reference's resolution status. The backend
  // (reference_resolver.py) emits the raw enum resolved | partial | unresolved;
  // surface the human phrase a reviewer expects instead of the code token.
  function referenceStatusLabel(status) {
    const labels = {
      resolved: "Resolved",
      partial: "Partially resolved",
      unresolved: "Unresolved",
    };
    return labels[String(status || "").toLowerCase()] || "Unknown";
  }

  function buildStructureFromParagraphs(paragraphs) {
    const documentParagraphs = paragraphs.filter((paragraph) => String(paragraph?.text || "").trim());
    const candidates = documentParagraphs
      .map((paragraph, position) => candidateForParagraph(paragraph, position))
      .filter(Boolean);
    const sections = [];

    if (documentParagraphs.length && (!candidates.length || candidates[0].position > 0)) {
      const end = candidates.length ? candidates[0].position : documentParagraphs.length;
      if (end > 0) {
        sections.push(sectionFromParagraphs({
          confidence: "high",
          heading: "Preamble",
          headingText: "Preamble",
          kind: "preamble",
          label: "Preamble",
          level: 0,
          number: null,
          paragraphs: documentParagraphs.slice(0, end),
          parentId: null,
          sectionId: "section-1",
        }));
      }
    }

    candidates.forEach((candidate, index) => {
      const nextPosition = candidates[index + 1]?.position ?? documentParagraphs.length;
      const sectionParagraphs = documentParagraphs.slice(candidate.position, nextPosition);
      if (!sectionParagraphs.length) return;
      const sectionId = `section-${sections.length + 1}`;
      sections.push(sectionFromParagraphs({
        ...candidate,
        paragraphs: sectionParagraphs,
        parentId: parentForCandidate(sections, candidate),
        sectionId,
      }));
    });

    const aliases = aliasesForSections(sections);
    const referenceIndex = referenceIndexForSections(sections, aliases);
    const mappedParagraphIds = new Set();
    sections.forEach((section) => (section.paragraph_ids || []).forEach((id) => mappedParagraphIds.add(String(id))));
    const allParagraphIds = new Set(documentParagraphs.map(paragraphId).filter(Boolean));
    return {
      aliases,
      reference_index: referenceIndex,
      sections,
      stats: {
        mapped_paragraph_count: mappedParagraphIds.size,
        section_count: sections.length,
        unmapped_paragraph_count: [...allParagraphIds].filter((id) => !mappedParagraphIds.has(id)).length,
        docx_heading_paragraph_count: documentParagraphs.filter((paragraph) => Number.isInteger(paragraph?.heading_level)).length,
        docx_numbered_paragraph_count: documentParagraphs.filter((paragraph) => paragraph?.numbering && typeof paragraph.numbering === "object").length,
        source_backed_section_count: sections.filter((section) => section.source && typeof section.source === "object").length,
        source_kinds: [...new Set(documentParagraphs.map((paragraph) => paragraph?.source_kind).filter(Boolean))].sort(),
        source_parts: [...new Set(documentParagraphs.map((paragraph) => paragraph?.source_part).filter(Boolean))].sort(),
        table_paragraph_count: documentParagraphs.filter((paragraph) => paragraph?.table && typeof paragraph.table === "object").length,
      },
      version: 2,
    };
  }

  function resolveReferencesFromParagraphs(paragraphs, structure) {
    const referenceIndex = structure?.reference_index || {};
    const aliasLookup = referenceIndex.alias_to_section_id || {};
    const sectionsById = referenceIndex.sections_by_id || {};
    const paragraphLookup = referenceIndex.paragraph_to_section_id || {};
    const references = [];
    paragraphs.forEach((paragraph) => {
      const paragraphText = String(paragraph?.text || "");
      const paragraphId = paragraphIdForReference(paragraph);
      const sourceSectionId = paragraphLookup[paragraphId] || null;
      const referenceRegex = new RegExp(`\\b(${referenceKindPattern()})\\s+(${explicitNumberPattern}(?:${referenceSeparatorPattern()}${explicitNumberPattern})*)(?=$|[^A-Za-z0-9])`, "gi");
      let match = referenceRegex.exec(paragraphText);
      while (match) {
        const kind = canonicalReferenceKind(match[1]);
        const numbers = referenceNumbers(match[2]);
        if (!kind || !numbers.length) {
          match = referenceRegex.exec(paragraphText);
          continue;
        }
        const items = numbers.map((number) => resolveReferenceItem(kind, number, aliasLookup, sectionsById));
        const resolvedSectionIds = dedupeStrings(items.map((item) => item.section_id).filter(Boolean));
        if (!isSelfHeadingReference(match.index, sourceSectionId, resolvedSectionIds)) {
          const unresolvedNumbers = items.filter((item) => !item.section_id).map((item) => item.number);
          references.push({
            id: `reference-${references.length + 1}`,
            items,
            kind,
            numbers,
            paragraph_id: paragraphId,
            paragraph_index: paragraphIndex(paragraph),
            reference_text: match[0],
            resolved_section_ids: resolvedSectionIds,
            source_section_id: sourceSectionId,
            status: referenceStatus(items),
            targets: resolvedSectionIds.map((sectionId) => sectionsById[sectionId]).filter(Boolean),
            unresolved_numbers: unresolvedNumbers,
          });
        }
        match = referenceRegex.exec(paragraphText);
      }
    });
    const targetSectionIds = new Set();
    references.forEach((reference) => reference.resolved_section_ids.forEach((sectionId) => targetSectionIds.add(sectionId)));
    return {
      references,
      stats: {
        partial_reference_count: references.filter((reference) => reference.status === "partial").length,
        reference_count: references.length,
        resolved_reference_count: references.filter((reference) => reference.status === "resolved").length,
        target_section_count: targetSectionIds.size,
        unresolved_reference_count: references.filter((reference) => reference.status === "unresolved").length,
      },
      version: 1,
    };
  }

  function resolveReferenceItem(kind, number, aliasLookup, sectionsById) {
    const aliasKeys = [`${kind}:${String(number).toLowerCase()}`, `number:${String(number).toLowerCase()}`];
    const matchedAlias = aliasKeys.find((aliasKey) => aliasLookup[aliasKey]);
    const sectionId = matchedAlias ? aliasLookup[matchedAlias] : null;
    return {
      alias_keys: aliasKeys,
      label: sectionId && sectionsById[sectionId] ? sectionsById[sectionId].label || "" : "",
      matched_alias: matchedAlias || null,
      number,
      section_id: sectionId || null,
      status: sectionId ? "resolved" : "unresolved",
    };
  }

  function referenceKindPattern() {
    return "clause|clauses|article|articles|section|sections|schedule|schedules|annex|annexes|annexure|annexures|appendix|appendices";
  }

  function referenceSeparatorPattern() {
    return "(?:\\s*(?:,|;)\\s*(?:(?:and|or)\\s+)?|\\s+(?:and|or|&)\\s+)";
  }

  function referenceNumbers(value) {
    const numberRegex = new RegExp(`^${explicitNumberPattern}$`, "i");
    return String(value || "")
      .split(new RegExp(referenceSeparatorPattern(), "i"))
      .map((part) => part.trim())
      .filter((part) => numberRegex.test(part));
  }

  function canonicalReferenceKind(kind) {
    const key = String(kind || "").trim().toLowerCase();
    const aliases = {
      annex: "annex",
      annexes: "annex",
      annexure: "annexure",
      annexures: "annexure",
      appendices: "appendix",
      appendix: "appendix",
      article: "article",
      articles: "article",
      clause: "clause",
      clauses: "clause",
      schedule: "schedule",
      schedules: "schedule",
      section: "section",
      sections: "section",
    };
    return aliases[key] || "";
  }

  function referenceStatus(items) {
    const resolvedCount = items.filter((item) => item.section_id).length;
    if (resolvedCount && resolvedCount === items.length) return "resolved";
    if (resolvedCount) return "partial";
    return "unresolved";
  }

  function isSelfHeadingReference(matchIndex, sourceSectionId, resolvedSectionIds) {
    return matchIndex === 0
      && sourceSectionId
      && resolvedSectionIds.length
      && resolvedSectionIds.every((sectionId) => sectionId === sourceSectionId);
  }

  function dedupeStrings(values) {
    const seen = new Set();
    const results = [];
    values.forEach((value) => {
      if (!value || seen.has(value)) return;
      seen.add(value);
      results.push(value);
    });
    return results;
  }

  function paragraphIdForReference(paragraph) {
    return paragraphId(paragraph) || "";
  }

  function candidateForParagraph(paragraph, position) {
    const text = collapseWhitespace(paragraph.text || "");
    if (!text) return null;

    const metadataCandidate = candidateFromSourceMetadata(paragraph, position, text);
    if (metadataCandidate) return metadataCandidate;

    const explicit = text.match(explicitHeadingRegex);
    if (explicit) {
      const kind = explicit[1].toLowerCase();
      const number = explicit[2].replace(/\.$/, "");
      const separator = explicit[3];
      const heading = cleanHeading(explicit[4]) || displayKind(kind);
      if (!looksLikeExplicitHeading(number, heading, separator)) return null;
      return {
        confidence: "high",
        heading,
        headingText: preview(text),
        kind,
        label: `${displayKind(kind)} ${number}`,
        level: levelForNumber(number),
        number,
        position,
      };
    }

    const numbered = text.match(numberedHeadingRegex);
    if (numbered && looksLikeNumberedHeading(numbered[1], numbered[2])) {
      const number = numbered[1].replace(/\.$/, "");
      return {
        confidence: "high",
        heading: cleanHeading(numbered[2]),
        headingText: preview(text),
        kind: "numbered",
        label: number,
        level: levelForNumber(number),
        number,
        position,
      };
    }

    const uppercasePrefix = text.match(/^([A-Z][A-Z0-9 &,/()'".-]{2,90}):\s*(.+)$/);
    if (uppercasePrefix && looksLikeUppercaseHeading(uppercasePrefix[1])) {
      const heading = cleanHeading(uppercasePrefix[1]);
      return {
        confidence: "medium",
        heading,
        headingText: preview(text),
        kind: "heading",
        label: heading,
        level: 1,
        number: null,
        position,
      };
    }

    return null;
  }

  function candidateFromSourceMetadata(paragraph, position, text) {
    const number = sourceStructureNumber(paragraph);
    const source = sourceMetadata(paragraph);
    if (number) {
      return {
        confidence: "high",
        heading: cleanHeading(stripLeadingNumber(text, number)) || cleanHeading(text),
        headingText: preview(source?.structure_label ? `${source.structure_label} ${text}` : text),
        kind: "numbered",
        label: number,
        level: sourceNumberLevel(paragraph, number),
        number,
        position,
        source,
      };
    }
    if (Number.isInteger(paragraph?.heading_level) && paragraph.heading_level > 0) {
      const heading = cleanHeading(text);
      return {
        confidence: "high",
        heading,
        headingText: preview(text),
        kind: "heading",
        label: heading,
        level: paragraph.heading_level,
        number: null,
        position,
        source,
      };
    }
    return null;
  }

  function sectionFromParagraphs({
    confidence,
    heading,
    headingText,
    kind,
    label,
    level,
    number,
    paragraphs,
    parentId,
    sectionId,
    source,
  }) {
    const paragraphIds = paragraphs.map(paragraphId).filter(Boolean);
    const first = paragraphs[0] || {};
    const last = paragraphs[paragraphs.length - 1] || {};
    const section = {
      confidence,
      end_index: paragraphIndex(last),
      end_paragraph_id: paragraphId(last),
      heading,
      heading_text: headingText,
      id: sectionId,
      kind,
      label,
      level,
      number: number || null,
      paragraph_ids: paragraphIds,
      parent_id: parentId || null,
      start_index: paragraphIndex(first),
      start_paragraph_id: paragraphId(first),
    };
    if (source && typeof source === "object") section.source = source;
    return section;
  }

  function parentForCandidate(sections, candidate) {
    if (!candidate.number) return null;
    for (const parentNumber of parentNumberCandidates(candidate.number)) {
      const parent = findSectionByNumber(sections, parentNumber, candidate.level);
      if (parent) return parent.id;
    }
    for (let index = sections.length - 1; index >= 0; index -= 1) {
      const section = sections[index];
      if (
        section.number
        && candidate.number.startsWith(`${section.number}.`)
        && Number(section.level || 0) < candidate.level
      ) {
        return section.id;
      }
    }
    return null;
  }

  function parentNumberCandidates(number) {
    const candidates = [];
    const queue = [String(number || "")];
    const seen = new Set(queue);
    while (queue.length) {
      const current = queue.shift();
      immediateParentNumbers(current).forEach((parentNumber) => {
        if (!parentNumber || seen.has(parentNumber)) return;
        candidates.push(parentNumber);
        queue.push(parentNumber);
        seen.add(parentNumber);
      });
    }
    return candidates;
  }

  function immediateParentNumbers(number) {
    const parents = [];
    const normalizedNumber = String(number || "").trim();
    if (!normalizedNumber) return parents;
    const parentheticalMatch = normalizedNumber.match(/^(.*)\([A-Za-z0-9]+\)$/);
    if (parentheticalMatch) {
      parents.push(parentheticalMatch[1]);
    } else {
      const rawParts = normalizedNumber.split(".").filter(Boolean);
      if (rawParts.length) {
        const strippedLastPart = stripLetterSuffix(rawParts[rawParts.length - 1]);
        if (strippedLastPart) parents.push([...rawParts.slice(0, -1), strippedLastPart].join("."));
        if (rawParts.length > 1) parents.push(rawParts.slice(0, -1).join("."));
      }
    }
    return parents.filter((parent) => parent && parent !== normalizedNumber);
  }

  function findSectionByNumber(sections, parentNumber, candidateLevel) {
    for (let index = sections.length - 1; index >= 0; index -= 1) {
      const section = sections[index];
      if (section.number === parentNumber && Number(section.level || 0) < candidateLevel) return section;
    }
    return null;
  }

  function aliasesForSections(sections) {
    const aliases = [];
    const seen = new Set();
    sections.forEach((section) => {
      const keys = [];
      if (section.number) {
        keys.push(`number:${String(section.number).toLowerCase()}`);
        if (["annex", "annexure", "appendix", "article", "clause", "schedule", "section"].includes(section.kind)) {
          keys.push(`${section.kind}:${String(section.number).toLowerCase()}`);
        }
      }
      const headingKey = normalizeHeadingKey(section.heading || "");
      if (headingKey) keys.push(`heading:${headingKey}`);
      keys.forEach((key) => {
        if (seen.has(key)) return;
        aliases.push({ key, label: section.label, section_id: section.id });
        seen.add(key);
      });
    });
    return aliases;
  }

  function referenceIndexForSections(sections, aliases) {
    const sectionIds = [];
    const sectionsById = {};
    const paragraphToSectionId = {};
    sections.forEach((section) => {
      const sectionId = String(section.id || "");
      if (!sectionId) return;
      sectionIds.push(sectionId);
      sectionsById[sectionId] = resolverSectionRecord(section);
      (section.paragraph_ids || []).forEach((paragraphId) => {
        if (paragraphId) paragraphToSectionId[String(paragraphId)] = sectionId;
      });
    });
    const aliasToSectionId = {};
    aliases.forEach((alias) => {
      if (alias?.key && alias?.section_id) aliasToSectionId[String(alias.key)] = String(alias.section_id);
    });
    return {
      alias_to_section_id: aliasToSectionId,
      paragraph_to_section_id: paragraphToSectionId,
      section_ids: sectionIds,
      sections_by_id: sectionsById,
      version: 2,
    };
  }

  function resolverSectionRecord(section) {
    const record = {
      end_index: Number.isInteger(section.end_index) ? section.end_index : null,
      heading: String(section.heading || ""),
      id: String(section.id || ""),
      kind: String(section.kind || ""),
      label: String(section.label || ""),
      level: Number.isInteger(section.level) ? section.level : 0,
      number: section.number || null,
      paragraph_ids: (section.paragraph_ids || []).map((paragraphId) => String(paragraphId)),
      parent_id: section.parent_id || null,
      start_index: Number.isInteger(section.start_index) ? section.start_index : null,
    };
    if (section.source && typeof section.source === "object") record.source = section.source;
    return record;
  }

  function sourceMetadata(paragraph) {
    const source = {};
    ["source_kind", "style_id", "style_name", "heading_level", "outline_level", "structure_label"].forEach((key) => {
      if (paragraph?.[key] !== undefined && paragraph?.[key] !== null && String(paragraph[key]) !== "") {
        source[key] = paragraph[key];
      }
    });
    ["numbering", "table"].forEach((key) => {
      if (paragraph?.[key] && typeof paragraph[key] === "object") source[key] = paragraph[key];
    });
    if (Number.isInteger(paragraph?.source_index)) source.source_index = paragraph.source_index;
    if (paragraph?.source_part) source.source_part = paragraph.source_part;
    return Object.keys(source).length ? source : null;
  }

  function sourceStructureNumber(paragraph) {
    if (paragraph?.structure_number) return cleanSourceNumber(paragraph.structure_number);
    const numbering = paragraph?.numbering;
    if (!numbering || typeof numbering !== "object") return "";
    if (["bullet", "none"].includes(String(numbering.format || ""))) return "";
    return cleanSourceNumber(numbering.label || "");
  }

  function cleanSourceNumber(value) {
    const cleaned = String(value || "").replace(/^[^\w(]+|[^\w)]+$/g, "").trim();
    const numberRegex = new RegExp(`^${explicitNumberPattern}$`, "i");
    return numberRegex.test(cleaned) ? cleaned : "";
  }

  function sourceNumberLevel(paragraph, number) {
    if (Number.isInteger(paragraph?.numbering?.level)) return paragraph.numbering.level + 1;
    return levelForNumber(number);
  }

  function stripLeadingNumber(text, number) {
    return String(text || "").replace(new RegExp(`^\\s*${escapeRegExp(number)}(?:\\s*[:.)\\-\\u2013\\u2014]\\s*|\\s+)`), "");
  }

  function escapeRegExp(value) {
    return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function paragraphId(paragraph) {
    return paragraph?.id === undefined || paragraph?.id === null ? null : String(paragraph.id);
  }

  function paragraphIndex(paragraph) {
    return Number.isInteger(paragraph?.index) ? paragraph.index : null;
  }

  function displayKind(kind) {
    const labels = {
      annex: "Annex",
      annexure: "Annexure",
      appendix: "Appendix",
      article: "Article",
      clause: "Clause",
      schedule: "Schedule",
      section: "Section",
    };
    return labels[kind] || "Section";
  }

  function levelForNumber(number) {
    const parts = numberParts(number);
    if (!parts.length) return 1;
    return parts.length + parts.filter((part) => stripLetterSuffix(part)).length;
  }

  function numberParts(number) {
    const parts = [];
    String(number || "").split(".").forEach((rawPart) => {
      const part = rawPart.trim();
      if (!part) return;
      const prefix = part.replace(/\([A-Za-z0-9]+\)$/g, "").trim();
      if (prefix) parts.push(prefix);
      const parentheticalMatches = [...part.matchAll(/\(([A-Za-z0-9]+)\)/g)];
      parentheticalMatches.forEach((match) => parts.push(match[1]));
    });
    return parts;
  }

  function stripLetterSuffix(part) {
    const match = String(part || "").match(/^(\d+)[A-Za-z]+$/);
    return match ? match[1] : null;
  }

  function looksLikeNumberedHeading(number, heading) {
    const cleaned = cleanHeading(heading);
    if (!cleaned) return false;
    if (requiresStrictOutlineHeading(number)) return looksLikeShortHeading(cleaned);
    return cleaned.length <= 120 || cleaned.slice(0, 90).includes(":");
  }

  function looksLikeExplicitHeading(number, heading, separator) {
    const cleaned = cleanHeading(heading);
    if (!cleaned) return true;
    if (/^(?:and|or)\b/i.test(cleaned)) return false;
    if (String(separator || "").trim()) return cleaned.length <= 160 || cleaned.slice(0, 90).includes(":");
    if (requiresStrictOutlineHeading(number)) return looksLikeShortHeading(cleaned);
    return looksLikeShortHeading(cleaned) || !operativeSentenceRegex.test(cleaned);
  }

  function requiresStrictOutlineHeading(number) {
    const normalizedNumber = String(number || "").trim();
    if (!normalizedNumber) return false;
    if (normalizedNumber.includes("(") || normalizedNumber.includes(")")) return true;
    return !normalizedNumber.includes(".") && new RegExp(`^(?:${romanNumberPattern}|[A-Za-z])$`, "i").test(normalizedNumber);
  }

  function looksLikeShortHeading(text) {
    const cleaned = cleanHeading(text);
    if (!cleaned || cleaned.length > 90 || operativeSentenceRegex.test(cleaned)) return false;
    const words = cleaned.split(/\s+/).filter(Boolean);
    if (words.length > 8) return false;
    const firstAlpha = cleaned.split("").find((character) => /[A-Za-z]/.test(character));
    return Boolean(firstAlpha && firstAlpha === firstAlpha.toUpperCase());
  }

  function looksLikeUppercaseHeading(text) {
    const letters = cleanHeading(text).split("").filter((character) => /[A-Za-z]/.test(character));
    if (letters.length < 3) return false;
    return letters.filter((character) => character === character.toUpperCase()).length / letters.length >= 0.85;
  }

  function cleanHeading(text) {
    return collapseWhitespace(text).replace(/^[ .:-]+|[ .:-]+$/g, "");
  }

  function collapseWhitespace(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function normalizeHeadingKey(text) {
    return collapseWhitespace(String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " "));
  }

  function preview(text, limit = 220) {
    const collapsed = collapseWhitespace(text);
    return collapsed.length <= limit ? collapsed : `${collapsed.slice(0, limit - 3).trim()}...`;
  }

  return {
    render,
  };
}
