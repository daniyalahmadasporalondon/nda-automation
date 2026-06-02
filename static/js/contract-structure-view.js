function createContractStructureController({ state, root }) {
  const romanNumberPattern = "[IVXLCDM]{2,}";
  const identifierPartPattern = `(?:${romanNumberPattern}|[A-Za-z]|\\d+[A-Za-z]*)`;
  const explicitNumberPattern = `${identifierPartPattern}(?:\\.${identifierPartPattern})*`;
  const numberedNumberPattern = `(?:\\d+[A-Za-z]*|${romanNumberPattern})(?:\\.${identifierPartPattern})*`;
  const explicitHeadingRegex = new RegExp(`^(clause|article|section|schedule|annex|annexure|appendix)\\s+(${explicitNumberPattern})(?:\\s*[:.\\-\\u2013\\u2014]\\s*|\\s+)(.*)$`, "i");
  const numberedHeadingRegex = new RegExp(`^(${numberedNumberPattern})(?:\\s*[:.\\-\\u2013\\u2014]\\s*|\\s+)(.+)$`);

  function render() {
    if (!root) return;
    const structure = effectiveStructure();
    const sections = Array.isArray(structure?.sections) ? structure.sections : [];
    const aliases = Array.isArray(structure?.aliases) ? structure.aliases : [];
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
        ${summaryTile("Sections", stats.section_count ?? sections.length)}
        ${summaryTile("Mapped paragraphs", stats.mapped_paragraph_count ?? mappedParagraphCount(sections))}
        ${summaryTile("Unmapped paragraphs", stats.unmapped_paragraph_count ?? 0)}
      </div>

      <section class="structure-section-list" aria-label="Detected contract sections">
        ${sections.map(renderSection).join("")}
      </section>

      ${renderAliases(aliases)}
    `;
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
    return `
      <article class="structure-row" style="--structure-indent: ${indent}px">
        <span class="structure-level-marker" aria-hidden="true"></span>
        <div class="structure-row-main">
          <div class="structure-row-title">
            <strong>${escapeHtml(section.label || section.heading || "Section")}</strong>
            <span>${escapeHtml(kindLabel(section.kind))}</span>
          </div>
          <p>${escapeHtml(section.heading || section.heading_text || section.label || "Untitled section")}</p>
          <small>
            <span>${escapeHtml(paragraphs)}</span>
            <span>${escapeHtml(confidenceLabel(section.confidence))}</span>
            ${parent}
          </small>
        </div>
      </article>
    `;
  }

  function renderAliases(aliases) {
    if (!aliases.length) return "";
    return `
      <section class="structure-aliases" aria-label="Structure aliases">
        <h2>Resolver aliases</h2>
        <div>
          ${aliases.slice(0, 18).map((alias) => `
            <span class="structure-alias-chip">${escapeHtml(alias.key || "")}</span>
          `).join("")}
        </div>
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
      },
      version: 1,
    };
  }

  function candidateForParagraph(paragraph, position) {
    const text = collapseWhitespace(paragraph.text || "");
    if (!text) return null;

    const explicit = text.match(explicitHeadingRegex);
    if (explicit) {
      const kind = explicit[1].toLowerCase();
      const number = explicit[2].replace(/\.$/, "");
      const heading = cleanHeading(explicit[3]) || displayKind(kind);
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
    if (numbered && looksLikeNumberedHeading(numbered[2])) {
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
    const parts = numberParts(number);
    const parents = [];
    if (!parts.length) return parents;
    const strippedLastPart = stripLetterSuffix(parts[parts.length - 1]);
    if (strippedLastPart) parents.push([...parts.slice(0, -1), strippedLastPart].join("."));
    if (parts.length > 1) parents.push(parts.slice(0, -1).join("."));
    return parents.filter((parent) => parent && parent !== number);
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
      version: 1,
    };
  }

  function resolverSectionRecord(section) {
    return {
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
    return String(number || "").split(".").filter(Boolean);
  }

  function stripLetterSuffix(part) {
    const match = String(part || "").match(/^(\d+)[A-Za-z]+$/);
    return match ? match[1] : null;
  }

  function looksLikeNumberedHeading(heading) {
    const cleaned = cleanHeading(heading);
    return Boolean(cleaned) && (cleaned.length <= 120 || cleaned.slice(0, 90).includes(":"));
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
