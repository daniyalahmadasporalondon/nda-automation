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

  function open(anchor, { label = "Download format", sections = [] } = {}) {
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

  function renderChoice(choice) {
    const available = choice.available !== false && typeof choice.onSelect === "function";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "document-download-option";
    button.dataset.downloadFormat = String(choice.format || "").toLowerCase();
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
      ? choice.filename || choice.description || "Ready"
      : choice.unavailableReason || "Unavailable";
    button.appendChild(meta);

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
    return {
      available,
      contentType: option?.content_type || "",
      filename: option?.filename || "",
      format,
      label: label || formatLabel(format),
      onSelect,
      unavailableReason: available
        ? ""
        : option?.unavailable_reason || (!hasUrl && option?.available ? "Download URL unavailable" : unavailableReason || "Unavailable"),
      url: option?.download_url || "",
    };
  }

  function option(documentDownloads, section, format) {
    return documentDownloads?.[section]?.formats?.[format] || null;
  }

  return {
    close,
    contractChoice,
    open,
    option,
  };
})();

window.DocumentDownloadMenu = DocumentDownloadMenu;
