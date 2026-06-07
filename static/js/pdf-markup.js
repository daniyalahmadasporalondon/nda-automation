// Interactive PDF markup for the Review workstation's "Original" page-image view.
//
// The Original view (renderOriginalDocumentSurface) paints each PDF page as a
// faithful image inside `<figure class="review-render-page" data-review-render-page="{n}">
//   <div class="review-render-page-image"><img ...></div></figure>`. This
// controller overlays an absolutely-positioned annotation layer on top of EACH
// such page image, but ONLY while the Original view is mounted. It lets the user
// add comments / highlights / strikethroughs, see existing annotations, delete
// them, and download a server-rendered marked-up PDF.
//
// COORDINATE CONTRACT (the backend agrees to this exactly):
//   Annotations are stored/sent in NORMALIZED page coordinates, origin TOP-LEFT,
//   each component in [0,1]:  rect = { x, y, w, h }. A comment is a point (w=h=0).
//   - From a pointer event over a page image, using its displayed bounding box:
//       x = (clientX - r.left)/r.width ; y = (clientY - r.top)/r.height
//     (w,h are drag-box fractions of the same box). All clamped to [0,1].
//   - To render an annotation back, multiply by the image's CURRENT displayed
//       size: left = x*r.width, top = y*r.height, width = w*r.width, height = h*r.height
//     so overlays stay correct across resize / zoom.
//
// API CONTRACT (backend implements):
//   GET    /api/matters/{id}/pdf-annotations         -> { annotations: [...] }
//   POST   /api/matters/{id}/pdf-annotations         -> 201 { annotation: {...} }
//   DELETE /api/matters/{id}/pdf-annotations/{annId} -> 200 { ok: true }
//   GET    /api/matters/{id}/marked-up-pdf           -> application/pdf (download)

function createPdfMarkupController({
  state,
  downloadBlob,
  escapeHtml,
  getSurfaceRoot,
  matterIsPdf,
}) {
  const TOOLS = [
    { id: "cursor", label: "Cursor", title: "Cursor (no markup)" },
    { id: "comment", label: "Comment", title: "Add a comment pin" },
    { id: "highlight", label: "Highlight", title: "Draw a highlight box" },
    { id: "strikethrough", label: "Strikethrough", title: "Strike through a region" },
  ];
  // Drags shorter than this (as a fraction of the page) are ignored so a stray
  // click with the Highlight/Strikethrough tool does not create a zero-size box.
  const MIN_DRAG_FRACTION = 0.01;

  const markup = {
    activeTool: "cursor",
    annotations: [],
    loadedMatterId: null,
    loadSequence: 0,
    mounted: false,
    openPopoverId: null,
  };

  let toolbarNode = null;
  // Live drag being drawn (highlight / strikethrough), or null.
  let activeDrag = null;
  // The page element currently hosting an open inline comment composer, or null.
  let commentComposer = null;

  function root() {
    return typeof getSurfaceRoot === "function" ? getSurfaceRoot() : null;
  }

  function selectedMatterId() {
    return state.selectedMatter && state.selectedMatter.id ? String(state.selectedMatter.id) : "";
  }

  function isPdfMatter() {
    return typeof matterIsPdf === "function" ? Boolean(matterIsPdf()) : Boolean(selectedMatterId());
  }

  function escape(value) {
    // Resolve escapeHtml lazily: in the browser it is bridged onto window by a
    // deferred module that runs AFTER this controller is constructed, so a value
    // captured at construction time could be undefined. Calling at runtime is
    // always safe. The injected `escapeHtml` (tests) takes precedence.
    const fn = typeof escapeHtml === "function"
      ? escapeHtml
      : (typeof window !== "undefined" && typeof window.escapeHtml === "function" ? window.escapeHtml : null);
    if (fn) return fn(value);
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function clamp01(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    if (number < 0) return 0;
    if (number > 1) return 1;
    return number;
  }

  // ---- entry points called from the render funnel -------------------------

  // The Original page-image surface has just (re)rendered. Mount the toolbar +
  // overlay layers and load annotations for the current matter.
  function onOriginalSurfaceRendered() {
    const surface = root();
    if (!surface) return;
    const matterId = selectedMatterId();
    if (!matterId || !isPdfMatter()) {
      // Original view is up but there's nothing to annotate (no PDF matter):
      // make sure no stale toolbar/overlays linger.
      teardown();
      return;
    }
    markup.mounted = true;
    ensureToolbar();
    mountPageLayers();
    // (Re)fetch only when the matter changed; a resize-only re-render keeps the
    // already-loaded annotations and just re-positions them below.
    if (markup.loadedMatterId !== matterId) {
      markup.annotations = [];
      loadAnnotations(matterId);
    } else {
      renderAllOverlays();
    }
  }

  // Leaving the Original view (any other view mode, or no render). Hide the
  // toolbar and drop every overlay so they never bleed into the other modes.
  function onLeaveOriginal() {
    if (!markup.mounted && !toolbarNode) return;
    teardown();
  }

  function teardown() {
    markup.mounted = false;
    closeCommentComposer();
    closePopover();
    activeDrag = null;
    if (toolbarNode && toolbarNode.parentNode) toolbarNode.parentNode.removeChild(toolbarNode);
    toolbarNode = null;
    // The page-image surface itself is owned by the rendering module and gets
    // replaced wholesale on the next render, so we only need to drop our own
    // injected layers when the surface is still present.
    const surface = root();
    if (surface) {
      surface.querySelectorAll("[data-pdf-markup-layer]").forEach((layer) => layer.remove());
      surface.querySelectorAll(".review-render-page-image.pdf-markup-host")
        .forEach((host) => host.classList.remove("pdf-markup-host"));
    }
  }

  // ---- toolbar -------------------------------------------------------------

  function ensureToolbar() {
    const surface = root();
    if (!surface) return;
    if (toolbarNode && toolbarNode.isConnected) {
      // Surface was re-rendered around an existing toolbar reference — re-attach.
      if (toolbarNode.parentNode !== surface) surface.insertBefore(toolbarNode, surface.firstChild);
      updateToolbarActiveState();
      return;
    }
    toolbarNode = document.createElement("div");
    toolbarNode.className = "pdf-markup-toolbar";
    toolbarNode.setAttribute("data-pdf-markup-toolbar", "");
    toolbarNode.setAttribute("role", "group");
    toolbarNode.setAttribute("aria-label", "PDF markup tools");
    toolbarNode.innerHTML = `
      <div class="pdf-markup-tools" role="group" aria-label="Markup tool">
        ${TOOLS.map((tool) => `
          <button
            type="button"
            class="pdf-markup-tool"
            data-pdf-markup-tool="${escape(tool.id)}"
            aria-pressed="${tool.id === markup.activeTool ? "true" : "false"}"
            title="${escape(tool.title)}"
          >${escape(tool.label)}</button>
        `).join("")}
      </div>
      <button type="button" class="pdf-markup-download" data-pdf-markup-download title="Download marked-up PDF">
        Download marked-up PDF
      </button>
    `;
    surface.insertBefore(toolbarNode, surface.firstChild);
    toolbarNode.querySelectorAll("[data-pdf-markup-tool]").forEach((button) => {
      button.addEventListener("click", () => setActiveTool(button.dataset.pdfMarkupTool));
    });
    toolbarNode.querySelector("[data-pdf-markup-download]")
      .addEventListener("click", downloadMarkedUpPdf);
  }

  function setActiveTool(toolId) {
    if (!TOOLS.some((tool) => tool.id === toolId)) return;
    markup.activeTool = toolId;
    // Switching tools cancels an in-progress comment composer or drag.
    closeCommentComposer();
    activeDrag = null;
    updateToolbarActiveState();
    updatePageInteractivity();
  }

  function updateToolbarActiveState() {
    if (!toolbarNode) return;
    toolbarNode.querySelectorAll("[data-pdf-markup-tool]").forEach((button) => {
      const active = button.dataset.pdfMarkupTool === markup.activeTool;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.classList.toggle("active", active);
    });
  }

  // ---- page layers ---------------------------------------------------------

  function pageElements() {
    const surface = root();
    if (!surface) return [];
    return Array.from(surface.querySelectorAll("[data-review-render-page]"));
  }

  function pageImageHost(pageEl) {
    return pageEl ? pageEl.querySelector(".review-render-page-image") : null;
  }

  function pageImage(pageEl) {
    const host = pageImageHost(pageEl);
    return host ? host.querySelector("img") : null;
  }

  function pageNumberOf(pageEl) {
    return positiveInt(pageEl && pageEl.dataset ? pageEl.dataset.reviewRenderPage : null);
  }

  // Attach an absolutely-positioned overlay layer + pointer handlers to each
  // page image. The layer is a child of `.review-render-page-image` (which is
  // the element whose bounding box defines the displayed page coordinates).
  function mountPageLayers() {
    pageElements().forEach((pageEl) => {
      const host = pageImageHost(pageEl);
      if (!host) return;
      host.classList.add("pdf-markup-host");
      let layer = host.querySelector("[data-pdf-markup-layer]");
      if (!layer) {
        layer = document.createElement("div");
        layer.className = "pdf-markup-layer";
        layer.setAttribute("data-pdf-markup-layer", "");
        host.appendChild(layer);
        bindPagePointer(pageEl, host, layer);
      }
    });
    updatePageInteractivity();
  }

  // The layer only intercepts pointer events while a draw tool is active; with
  // the Cursor tool the page image is untouched (text selection etc. unaffected).
  function updatePageInteractivity() {
    const drawing = markup.activeTool === "comment"
      || markup.activeTool === "highlight"
      || markup.activeTool === "strikethrough";
    pageElements().forEach((pageEl) => {
      const host = pageImageHost(pageEl);
      const layer = host ? host.querySelector("[data-pdf-markup-layer]") : null;
      if (layer) layer.classList.toggle("interactive", drawing);
      if (host) host.classList.toggle("pdf-markup-tool-comment", markup.activeTool === "comment");
    });
  }

  function bindPagePointer(pageEl, host, layer) {
    layer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const tool = markup.activeTool;
      if (tool === "comment") {
        event.preventDefault();
        openCommentComposer(pageEl, host, pointFromEvent(host, event));
        return;
      }
      if (tool === "highlight" || tool === "strikethrough") {
        event.preventDefault();
        startDrag(pageEl, host, layer, tool, event);
      }
    });
  }

  // Normalized point (top-left origin) for a single pointer event.
  function pointFromEvent(host, event) {
    const rect = host.getBoundingClientRect();
    return {
      x: rect.width ? clamp01((event.clientX - rect.left) / rect.width) : 0,
      y: rect.height ? clamp01((event.clientY - rect.top) / rect.height) : 0,
    };
  }

  // ---- drag (highlight / strikethrough) -----------------------------------

  function startDrag(pageEl, host, layer, tool, downEvent) {
    closeCommentComposer();
    closePopover();
    const start = pointFromEvent(host, downEvent);
    const preview = document.createElement("div");
    preview.className = `pdf-markup-overlay pdf-markup-${tool} pdf-markup-preview`;
    layer.appendChild(preview);
    activeDrag = { host, layer, pageEl, preview, start, tool };
    try {
      layer.setPointerCapture(downEvent.pointerId);
    } catch (error) {
      // setPointerCapture can throw if the pointer is already released; the
      // window-level listeners below still complete the drag.
    }

    const onMove = (event) => {
      if (!activeDrag) return;
      const current = pointFromEvent(host, event);
      positionPreview(activeDrag, current);
    };
    const onUp = (event) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (!activeDrag) return;
      const drag = activeDrag;
      activeDrag = null;
      const current = pointFromEvent(host, event);
      const box = rectFromPoints(drag.start, current);
      if (drag.preview.parentNode) drag.preview.parentNode.removeChild(drag.preview);
      if (box.w < MIN_DRAG_FRACTION || box.h < MIN_DRAG_FRACTION) return;
      createAnnotation({
        page: pageNumberOf(drag.pageEl),
        rect: box,
        type: drag.tool,
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function rectFromPoints(a, b) {
    const x = Math.min(a.x, b.x);
    const y = Math.min(a.y, b.y);
    const w = Math.abs(a.x - b.x);
    const h = Math.abs(a.y - b.y);
    return { h: clamp01(h), w: clamp01(w), x: clamp01(x), y: clamp01(y) };
  }

  function positionPreview(drag, current) {
    const rect = drag.host.getBoundingClientRect();
    const box = rectFromPoints(drag.start, current);
    applyBoxStyle(drag.preview, box, rect, drag.tool);
  }

  // ---- annotation rendering ------------------------------------------------

  function renderAllOverlays() {
    const surface = root();
    if (!surface) return;
    pageElements().forEach((pageEl) => {
      const host = pageImageHost(pageEl);
      const layer = host ? host.querySelector("[data-pdf-markup-layer]") : null;
      if (!layer) return;
      // Drop previously-rendered annotations (keep an in-flight drag preview).
      layer.querySelectorAll("[data-annotation-id]").forEach((node) => node.remove());
      const pageNumber = pageNumberOf(pageEl);
      const rect = host.getBoundingClientRect();
      markup.annotations
        .filter((annotation) => annotation.page === pageNumber)
        .forEach((annotation) => layer.appendChild(buildOverlayNode(annotation, rect)));
    });
  }

  function buildOverlayNode(annotation, rect) {
    if (annotation.type === "comment") return buildCommentPin(annotation, rect);
    return buildBoxOverlay(annotation, rect);
  }

  function buildCommentPin(annotation, rect) {
    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "pdf-markup-pin";
    pin.setAttribute("data-annotation-id", String(annotation.id));
    pin.setAttribute("data-annotation-type", "comment");
    pin.title = "View comment";
    pin.setAttribute("aria-label", "View comment");
    pin.textContent = "💬";
    pin.style.left = `${annotation.rect.x * rect.width}px`;
    pin.style.top = `${annotation.rect.y * rect.height}px`;
    // Keep pin interactions off the drawing layer beneath it.
    pin.addEventListener("pointerdown", (event) => event.stopPropagation());
    pin.addEventListener("click", (event) => {
      event.stopPropagation();
      togglePopover(annotation, pin);
    });
    return pin;
  }

  function buildBoxOverlay(annotation, rect) {
    const node = document.createElement("div");
    node.className = `pdf-markup-overlay pdf-markup-${annotation.type === "strikethrough" ? "strikethrough" : "highlight"}`;
    node.setAttribute("data-annotation-id", String(annotation.id));
    node.setAttribute("data-annotation-type", annotation.type);
    applyBoxStyle(node, annotation.rect, rect, annotation.type);
    if (annotation.color) node.style.setProperty("--pdf-markup-color", String(annotation.color));
    if (annotation.type === "strikethrough") {
      node.innerHTML = '<span class="pdf-markup-strike-line" aria-hidden="true"></span>';
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "pdf-markup-delete";
    remove.setAttribute("data-annotation-delete", String(annotation.id));
    remove.title = "Delete annotation";
    remove.setAttribute("aria-label", "Delete annotation");
    remove.textContent = "×";
    remove.addEventListener("pointerdown", (event) => event.stopPropagation());
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteAnnotation(annotation.id);
    });
    node.appendChild(remove);
    return node;
  }

  // Position a box overlay/preview from a normalized rect against the page's
  // CURRENT displayed bounding box (so it tracks resize).
  function applyBoxStyle(node, box, rect, type) {
    node.style.left = `${box.x * rect.width}px`;
    node.style.top = `${box.y * rect.height}px`;
    node.style.width = `${box.w * rect.width}px`;
    node.style.height = `${box.h * rect.height}px`;
    if (type === "strikethrough") node.classList.add("pdf-markup-strikethrough");
  }

  // ---- comment composer + popover -----------------------------------------

  function openCommentComposer(pageEl, host, point) {
    closeCommentComposer();
    closePopover();
    const composer = document.createElement("div");
    composer.className = "pdf-markup-composer";
    composer.setAttribute("data-pdf-markup-composer", "");
    composer.style.left = `${point.x * host.getBoundingClientRect().width}px`;
    composer.style.top = `${point.y * host.getBoundingClientRect().height}px`;
    composer.innerHTML = `
      <textarea class="pdf-markup-composer-input" data-pdf-markup-comment-input rows="3"
        placeholder="Add a comment" aria-label="Comment text"></textarea>
      <div class="pdf-markup-composer-actions">
        <button type="button" class="pdf-markup-composer-confirm" data-pdf-markup-comment-confirm>Add</button>
        <button type="button" class="pdf-markup-composer-cancel" data-pdf-markup-comment-cancel>Cancel</button>
      </div>
    `;
    const layer = host.querySelector("[data-pdf-markup-layer]");
    (layer || host).appendChild(composer);
    commentComposer = { composer, host, pageEl, point };
    // The composer sits on top of the interactive layer; swallow pointer events
    // inside it so a click on its buttons does not also fire the layer's
    // pointerdown (which would re-open a fresh composer and cancel the click).
    composer.addEventListener("pointerdown", (event) => event.stopPropagation());

    const input = composer.querySelector("[data-pdf-markup-comment-input]");
    const confirm = () => {
      const text = String(input.value || "").trim();
      if (!text) {
        input.focus();
        return;
      }
      const page = pageNumberOf(pageEl);
      const pinPoint = commentComposer.point;
      closeCommentComposer();
      createAnnotation({
        page,
        rect: { h: 0, w: 0, x: pinPoint.x, y: pinPoint.y },
        text,
        type: "comment",
      });
    };
    composer.querySelector("[data-pdf-markup-comment-confirm]").addEventListener("click", confirm);
    composer.querySelector("[data-pdf-markup-comment-cancel]").addEventListener("click", closeCommentComposer);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeCommentComposer();
      } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        confirm();
      }
    });
    input.focus();
  }

  function closeCommentComposer() {
    if (commentComposer && commentComposer.composer.parentNode) {
      commentComposer.composer.parentNode.removeChild(commentComposer.composer);
    }
    commentComposer = null;
  }

  function togglePopover(annotation, pinNode) {
    if (markup.openPopoverId === annotation.id) {
      closePopover();
      return;
    }
    closePopover();
    const popover = document.createElement("div");
    popover.className = "pdf-markup-popover";
    popover.setAttribute("data-pdf-markup-popover", String(annotation.id));
    popover.innerHTML = `
      <p class="pdf-markup-popover-text">${escape(annotation.text || "")}</p>
      <div class="pdf-markup-popover-actions">
        <button type="button" class="pdf-markup-popover-delete" data-pdf-markup-popover-delete>Delete</button>
      </div>
    `;
    popover.addEventListener("pointerdown", (event) => event.stopPropagation());
    popover.querySelector("[data-pdf-markup-popover-delete]").addEventListener("click", (event) => {
      event.stopPropagation();
      deleteAnnotation(annotation.id);
    });
    pinNode.insertAdjacentElement("afterend", popover);
    markup.openPopoverId = annotation.id;
  }

  function closePopover() {
    const surface = root();
    if (surface) surface.querySelectorAll("[data-pdf-markup-popover]").forEach((node) => node.remove());
    markup.openPopoverId = null;
  }

  // ---- API calls -----------------------------------------------------------

  function loadAnnotations(matterId) {
    const sequence = markup.loadSequence + 1;
    markup.loadSequence = sequence;
    fetch(`/api/matters/${encodeURIComponent(matterId)}/pdf-annotations`)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error("Annotations unavailable"))))
      .then((payload) => {
        if (sequence !== markup.loadSequence || selectedMatterId() !== matterId) return;
        markup.annotations = normalizeAnnotations(payload && payload.annotations);
        markup.loadedMatterId = matterId;
        if (markup.mounted) renderAllOverlays();
      })
      .catch(() => {
        if (sequence !== markup.loadSequence || selectedMatterId() !== matterId) return;
        markup.annotations = [];
        markup.loadedMatterId = matterId;
        if (markup.mounted) renderAllOverlays();
      });
  }

  function createAnnotation({ page, type, rect, text, color }) {
    const matterId = selectedMatterId();
    if (!matterId || !page) return;
    const body = { page, rect, type };
    if (text != null) body.text = String(text);
    if (color != null) body.color = String(color);
    fetch(`/api/matters/${encodeURIComponent(matterId)}/pdf-annotations`, {
      body: JSON.stringify(body),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error("Could not save annotation"))))
      .then((payload) => {
        if (selectedMatterId() !== matterId) return;
        const annotation = normalizeAnnotation(payload && payload.annotation);
        if (!annotation) return;
        markup.annotations.push(annotation);
        if (markup.mounted) renderAllOverlays();
      })
      .catch(() => {
        /* Surface stays as-is; a failed save simply leaves no overlay. */
      });
  }

  function deleteAnnotation(annotationId) {
    const matterId = selectedMatterId();
    if (!matterId || annotationId == null) return;
    const id = String(annotationId);
    fetch(`/api/matters/${encodeURIComponent(matterId)}/pdf-annotations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error("Could not delete annotation"))))
      .then(() => {
        if (selectedMatterId() !== matterId) return;
        markup.annotations = markup.annotations.filter((annotation) => String(annotation.id) !== id);
        if (markup.openPopoverId != null && String(markup.openPopoverId) === id) closePopover();
        if (markup.mounted) renderAllOverlays();
      })
      .catch(() => {
        /* Leave the overlay in place if the delete failed. */
      });
  }

  function downloadMarkedUpPdf() {
    const matterId = selectedMatterId();
    if (!matterId) return;
    const button = toolbarNode ? toolbarNode.querySelector("[data-pdf-markup-download]") : null;
    if (button) button.disabled = true;
    fetch(`/api/matters/${encodeURIComponent(matterId)}/marked-up-pdf`)
      .then((response) => (response.ok ? response.blob() : Promise.reject(new Error("Marked-up PDF unavailable"))))
      .then((blob) => {
        if (typeof downloadBlob === "function") downloadBlob(blob, markedUpFilename(matterId));
      })
      .catch(() => {
        /* No download on failure; button is re-enabled below. */
      })
      .finally(() => {
        if (button) button.disabled = false;
      });
  }

  function markedUpFilename(matterId) {
    const base = String(state.selectedMatter && (state.selectedMatter.source_filename
      || state.selectedMatter.attachment_filename || "") || "").trim();
    const stem = base.replace(/\.[^.]*$/, "");
    const safe = Array.from(stem || `matter-${matterId}`)
      .map((character) => (/[a-z0-9_-]/i.test(character) ? character : "-"))
      .join("")
      .replace(/^[-_]+/g, "")
      .replace(/[-_]+$/g, "");
    return `${safe || "nda"}-marked-up.pdf`;
  }

  // ---- normalization -------------------------------------------------------

  function normalizeAnnotations(list) {
    if (!Array.isArray(list)) return [];
    return list.map(normalizeAnnotation).filter(Boolean);
  }

  function normalizeAnnotation(raw) {
    if (!raw || typeof raw !== "object") return null;
    const type = String(raw.type || "").trim().toLowerCase();
    if (!["comment", "highlight", "strikethrough"].includes(type)) return null;
    const page = positiveInt(raw.page);
    if (!page) return null;
    const rect = normalizeRect(raw.rect, type);
    const annotation = {
      id: raw.id == null ? `local-${Date.now()}-${Math.random().toString(36).slice(2)}` : String(raw.id),
      page,
      rect,
      type,
    };
    if (raw.text != null) annotation.text = String(raw.text);
    if (raw.color != null) annotation.color = String(raw.color);
    if (raw.author != null) annotation.author = String(raw.author);
    if (raw.created_at != null) annotation.created_at = String(raw.created_at);
    return annotation;
  }

  function normalizeRect(rect, type) {
    const source = rect && typeof rect === "object" ? rect : {};
    const normalized = {
      h: clamp01(source.h),
      w: clamp01(source.w),
      x: clamp01(source.x),
      y: clamp01(source.y),
    };
    if (type === "comment") {
      normalized.w = 0;
      normalized.h = 0;
    }
    return normalized;
  }

  function positiveInt(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
  }

  // Re-position every overlay against the pages' current displayed sizes.
  function reposition() {
    if (!markup.mounted) return;
    closeCommentComposer();
    renderAllOverlays();
  }

  // Keep overlays aligned when the layout reflows (window resize, zoom).
  window.addEventListener("resize", reposition);

  return {
    onLeaveOriginal,
    onOriginalSurfaceRendered,
    // Exposed for tests / programmatic control.
    reposition,
    setActiveTool,
    state: markup,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createPdfMarkupController };
}
