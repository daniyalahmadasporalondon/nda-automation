function createContractStructureController({ state, root }) {
  function render() {
    if (!root) return;
    const structure = state.latestReviewResult?.contract_structure;
    const sections = Array.isArray(structure?.sections) ? structure.sections : [];
    const aliases = Array.isArray(structure?.aliases) ? structure.aliases : [];
    const stats = structure?.stats || {};

    if (!state.latestReviewResult) {
      root.innerHTML = '<div class="structure-empty">Load or review an NDA to generate its structure map.</div>';
      return;
    }

    if (!sections.length) {
      root.innerHTML = '<div class="structure-empty">No contract sections were detected for this review.</div>';
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

  return {
    render,
  };
}
