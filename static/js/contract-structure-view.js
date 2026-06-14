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
      root.innerHTML = '<div class="structure-empty">This review does not include a structure map yet. Reload the matter or run the review again to generate it.</div>';
      return;
    }

    if (!sections.length) {
      root.innerHTML = '<div class="structure-empty">No section headings were detected in this review. The parser could not identify clauses, articles, numbered headings, or uppercase section labels.</div>';
      return;
    }

    root.innerHTML = `
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

      <section class="structure-section-list" aria-label="Detected contract sections">
        ${sections.map(renderSection).join("")}
      </section>

      ${renderReferences(references)}
    `;
    bindStructureRowKeyboard();
  }

  // The global delegated [data-para-ref] handler (app.js) covers row CLICKS for free,
  // but it is click-only. Keep the keyboard-accessible rows usable by translating
  // Enter/Space on a focused row into the same jumpToParagraph the click path uses.
  // Rebound per render because render() replaces root.innerHTML wholesale.
  function bindStructureRowKeyboard() {
    if (!root) return;
    root.querySelectorAll(".structure-row-nav[data-para-ref]").forEach((row) => {
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
        event.preventDefault();
        const paragraphId = row.getAttribute("data-para-ref");
        if (paragraphId && typeof jumpToParagraph === "function") jumpToParagraph(paragraphId);
      });
    });
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

  function renderSection(section) {
    const level = Math.max(0, Math.min(5, Number(section.level || 0)));
    const indent = 14 + (level * 16);
    const paragraphs = paragraphRangeLabel(section);
    const parent = section.parent_id ? `<span>Parent ${escapeHtml(section.parent_id)}</span>` : "";
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
    return `
      <article class="structure-row${navClass}" style="--structure-indent: ${indent}px"${navAttrs}>
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
            ${parent}
          </small>
        </div>
      </article>
    `;
  }

  function renderReferences(references) {
    if (!references.length) return "";
    return `
      <section class="structure-references" aria-label="Resolved references">
        <h2>Resolved references</h2>
        ${references.slice(0, 12).map((reference) => {
          const targets = (reference.targets || []).map((target) => target.label).filter(Boolean).join(", ");
          const unresolved = (reference.unresolved_numbers || []).length
            ? `Unresolved ${reference.unresolved_numbers.join(", ")}`
            : "";
          return `
            <article class="structure-reference-row">
              <strong>${escapeHtml(reference.reference_text || "Reference")}</strong>
              <span>${escapeHtml(targets || unresolved || "No target")}</span>
              <small>
                <span>${escapeHtml(paragraphRangeLabel({
                  start_index: reference.paragraph_index,
                  end_index: reference.paragraph_index,
                  start_paragraph_id: reference.paragraph_id,
                }))}</span>
                <span>${escapeHtml(reference.status || "unknown")}</span>
              </small>
            </article>
          `;
        }).join("")}
      </section>
    `;
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

  function sourceSummary(source) {
    if (!source || typeof source !== "object") return "";
    if (source.numbering?.label) return `Word number ${source.numbering.label}`;
    if (source.style_name) return `Style ${source.style_name}`;
    if (source.table) {
      const table = source.table;
      return `Table ${table.table_index || "?"}, row ${table.row_index || "?"}, cell ${table.cell_index || "?"}`;
    }
    if (source.source_part) return `Source ${source.source_part}`;
    return source.source_kind ? `Source ${source.source_kind}` : "";
  }

  function paragraphRangeLabel(section) {
    const start = section.start_index ?? section.start_paragraph_id;
    const end = section.end_index ?? section.end_paragraph_id;
    if (start === undefined && end === undefined) return "No paragraph range";
    if (start === end || end === undefined) return `Paragraph ${start}`;
    return `Paragraphs ${start}-${end}`;
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
    return labels[kind] || "Section";
  }

  function confidenceLabel(confidence) {
    return `${confidence || "unknown"} confidence`;
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
