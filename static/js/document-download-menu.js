const DocumentDownloadMenu = (() => {
  let activeMenu = null;
  let activeAnchor = null;
  let outsideHandler = null;
  let keyHandler = null;

  function close() {
    if (outsideHandler) {
      document.removeEventListener("pointerdown", outsideHandler, true);
      outsideHandler = null;
    }
    if (keyHandler) {
      document.removeEventListener("keydown", keyHandler, true);
      keyHandler = null;
    }
    if (activeAnchor) activeAnchor.setAttribute("aria-expanded", "false");
    activeMenu?.remove();
    activeMenu = null;
    activeAnchor = null;
  }

  function open(anchor, { label = "Download format", sections = [], preview = null } = {}) {
    close();
    const normalizedSections = sections
      .map((section) => ({
        ...section,
        choices: (section.choices || []).filter(Boolean),
      }))
      .filter((section) => section.choices.length);
    if (!anchor || !normalizedSections.length) return;

    const menu = document.createElement("div");
    menu.className = "document-download-menu";
    menu.dataset.documentDownloadMenu = "true";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", label);

    // Optional contents preview, rendered BEFORE the format choices so the
    // reviewer sees what the export will include before picking a format.
    // Backward-compatible: callers that omit `preview` get the original menu.
    const previewNode = renderPreview(preview);
    if (previewNode) menu.appendChild(previewNode);

    normalizedSections.forEach((section) => {
      const group = document.createElement("section");
      group.className = "document-download-group";
      if (section.label) {
        const heading = document.createElement("p");
        heading.className = "document-download-group-label";
        heading.textContent = section.label;
        group.appendChild(heading);
      }
      section.choices.forEach((choice) => {
        group.appendChild(renderChoice(choice));
      });
      menu.appendChild(group);
    });

    document.body.appendChild(menu);
    positionMenu(menu, anchor);
    anchor.setAttribute("aria-haspopup", "menu");
    anchor.setAttribute("aria-expanded", "true");
    activeMenu = menu;
    activeAnchor = anchor;

    outsideHandler = (event) => {
      if (activeMenu?.contains(event.target) || activeAnchor?.contains(event.target)) return;
      close();
    };
    keyHandler = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close();
      activeAnchor?.focus();
    };
    document.addEventListener("pointerdown", outsideHandler, true);
    document.addEventListener("keydown", keyHandler, true);

    const firstEnabled = menu.querySelector(".document-download-option:not(:disabled)");
    firstEnabled?.focus();
  }

  // Render an optional contents-preview block: a small heading plus a bulleted
  // list of plain-text lines describing what the download will include. Returns
  // null when there is nothing to preview so the menu is unchanged for callers
  // that pass no preview. Lines are set via textContent (never innerHTML) so
  // caller-supplied summary text cannot inject markup.
  function renderPreview(preview) {
    const lines = (preview?.lines || []).map((line) => String(line || "").trim()).filter(Boolean);
    if (!lines.length) return null;
    const block = document.createElement("section");
    block.className = "document-download-preview";
    block.dataset.documentDownloadPreview = "true";
    const title = String(preview?.title || "").trim();
    if (title) {
      const heading = document.createElement("p");
      heading.className = "document-download-preview-title";
      heading.textContent = title;
      block.appendChild(heading);
    }
    const list = document.createElement("ul");
    list.className = "document-download-preview-list";
    lines.forEach((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    block.appendChild(list);
    return block;
  }

  function renderChoice(choice) {
    const available = choice.available !== false && typeof choice.onSelect === "function";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "document-download-option";
    button.dataset.downloadFormat = String(choice.format || "").toLowerCase();
    if (choice.sourceTransform) button.dataset.sourceTransform = String(choice.sourceTransform);
    button.setAttribute("role", "menuitem");
    button.disabled = !available;
    button.setAttribute("aria-disabled", available ? "false" : "true");

    const label = document.createElement("span");
    label.className = "document-download-option-label";
    label.textContent = choice.label || formatLabel(choice.format);
    button.appendChild(label);

    const meta = document.createElement("span");
    meta.className = "document-download-option-meta";
    meta.textContent = available
      ? choice.description || choice.filename || "Ready"
      : choice.unavailableReason || "Unavailable";
    button.appendChild(meta);

    const detailText = [choice.detail, choice.fidelity?.message].filter(Boolean).join(" · ");
    if (detailText) {
      const detail = document.createElement("span");
      detail.className = "document-download-option-detail";
      detail.textContent = detailText;
      button.appendChild(detail);
    }

    if (available) {
      button.addEventListener("click", async () => {
        close();
        await choice.onSelect(choice);
      });
    }
    return button;
  }

  function positionMenu(menu, anchor) {
    const rect = anchor.getBoundingClientRect();
    const width = Math.max(menu.offsetWidth || 220, 220);
    const left = Math.min(
      Math.max(12, rect.right - width),
      Math.max(12, window.innerWidth - width - 12),
    );
    const top = Math.min(rect.bottom + 8, Math.max(12, window.innerHeight - menu.offsetHeight - 12));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.style.minWidth = `${width}px`;
  }

  function formatLabel(format) {
    return String(format || "download").toUpperCase();
  }

  function contractChoice(option, { label, onSelect, unavailableReason } = {}) {
    const format = option?.format || label || "download";
    const hasUrl = Boolean(option?.download_url);
    const available = Boolean(option?.available && hasUrl && onSelect);
    const optionLabel = option?.label || label || formatLabel(format);
    return {
      available,
      contentType: option?.content_type || "",
      description: option?.label && option?.filename ? option.filename : "",
      detail: sourceTransformLabel(option?.source_transform),
      fidelity: option?.fidelity || null,
      filename: option?.filename || "",
      format,
      label: optionLabel,
      onSelect,
      sourceTransform: option?.source_transform || "",
      unavailableReason: available
        ? ""
        : option?.unavailable_reason || (!hasUrl && option?.available ? "Download URL unavailable" : unavailableReason || "Unavailable"),
      url: option?.download_url || "",
    };
  }

  function option(documentDownloads, section, format) {
    return documentDownloads?.[section]?.formats?.[format] || null;
  }

  function sourceTransformLabel(sourceTransform) {
    if (!sourceTransform) return "";
    const labels = {
      pdf_to_reconstructed_reviewed_docx: "PDF-to-Word reconstruction",
      pdf_to_reconstructed_docx: "PDF-to-Word reconstruction",
      docx_source_passthrough: "Source DOCX formatting",
      reviewed_pdf_annotations: "PDF annotation export",
    };
    return labels[sourceTransform] || String(sourceTransform).replace(/_/g, " ");
  }

  return {
    close,
    contractChoice,
    open,
    option,
  };
})();

window.DocumentDownloadMenu = DocumentDownloadMenu;
