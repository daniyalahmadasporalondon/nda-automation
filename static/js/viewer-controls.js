// Viewer toolbar controls: page scroll, zoom in/out, and full screen.
// Operates on the document pane (.studio-document) — vanilla, no deps.
(function () {
  "use strict";

  function initViewerControls() {
    const docPane = document.querySelector("#reviewView .studio-document");
    const scrollEl = document.querySelector("#reviewView .studio-page-wrap");
    const pageEl = document.querySelector("#reviewView .studio-page");
    const pagePrev = document.getElementById("studioPagePrev");
    const pageNext = document.getElementById("studioPageNext");
    const pageIndicator = document.getElementById("studioPageIndicator");
    const zoomOut = document.getElementById("studioZoomOut");
    const zoomIn = document.getElementById("studioZoomIn");
    const zoomLevel = document.getElementById("studioZoomLevel");
    const fullscreenBtn = document.getElementById("studioFullscreen");
    if (!scrollEl || !pageEl) return;

    // ---- Zoom (document zoom, like a PDF viewer) ----
    const ZOOM_MIN = 50;
    const ZOOM_MAX = 200;
    const ZOOM_STEP = 10;
    let zoom = 100;
    function applyZoom() {
      pageEl.style.zoom = String(zoom / 100);
      if (zoomLevel) zoomLevel.textContent = zoom + "%";
      if (zoomOut) zoomOut.disabled = zoom <= ZOOM_MIN;
      if (zoomIn) zoomIn.disabled = zoom >= ZOOM_MAX;
      updatePages();
    }
    if (zoomOut) {
      zoomOut.addEventListener("click", function () {
        zoom = Math.max(ZOOM_MIN, zoom - ZOOM_STEP);
        applyZoom();
      });
    }
    if (zoomIn) {
      zoomIn.addEventListener("click", function () {
        zoom = Math.min(ZOOM_MAX, zoom + ZOOM_STEP);
        applyZoom();
      });
    }

    // ---- Page scroll (viewport-pages over the continuously scrolled doc) ----
    function pageMetrics() {
      const vh = scrollEl.clientHeight || 1;
      const total = Math.max(1, Math.ceil(scrollEl.scrollHeight / vh));
      const current = Math.min(total, Math.floor(scrollEl.scrollTop / vh) + 1);
      return { vh: vh, total: total, current: current };
    }
    function updatePages() {
      if (!pageIndicator) return;
      const m = pageMetrics();
      pageIndicator.textContent = m.current + " / " + m.total;
      if (pagePrev) pagePrev.disabled = m.current <= 1;
      if (pageNext) pageNext.disabled = m.current >= m.total;
    }
    if (pagePrev) {
      pagePrev.addEventListener("click", function () {
        scrollEl.scrollBy({ top: -Math.round(scrollEl.clientHeight * 0.9), behavior: "smooth" });
      });
    }
    if (pageNext) {
      pageNext.addEventListener("click", function () {
        scrollEl.scrollBy({ top: Math.round(scrollEl.clientHeight * 0.9), behavior: "smooth" });
      });
    }
    let scrollRaf = 0;
    scrollEl.addEventListener("scroll", function () {
      if (scrollRaf) return;
      scrollRaf = requestAnimationFrame(function () {
        scrollRaf = 0;
        updatePages();
      });
    });
    window.addEventListener("resize", updatePages);
    // Recompute when the rendered document changes size (async review render).
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(updatePages).observe(pageEl);
    }

    // ---- Full screen ----
    function fsTarget() {
      return docPane || scrollEl;
    }
    function toggleFullscreen() {
      const el = fsTarget();
      if (!document.fullscreenElement) {
        if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
      } else if (document.exitFullscreen) {
        document.exitFullscreen().catch(function () {});
      }
    }
    if (fullscreenBtn) fullscreenBtn.addEventListener("click", toggleFullscreen);
    document.addEventListener("fullscreenchange", function () {
      const active = !!document.fullscreenElement;
      if (fullscreenBtn) fullscreenBtn.setAttribute("aria-pressed", active ? "true" : "false");
      updatePages();
    });

    function clearSelectionCommentAffordances() {
      document
        .querySelectorAll("#reviewView .studio-doc-paragraph.has-selection")
        .forEach(function (paragraph) {
          paragraph.classList.remove("has-selection");
          const tools = paragraph.querySelector(".paragraph-comment-tools");
          if (tools) tools.removeAttribute("style");
        });
    }

    function selectionRect(range) {
      const rects = Array.from(range.getClientRects()).filter(function (rect) {
        return rect.width > 0 && rect.height > 0;
      });
      return rects[rects.length - 1] || range.getBoundingClientRect();
    }

    function positionSelectionCommentButton(paragraph, range) {
      const tools = paragraph.querySelector(".paragraph-comment-tools");
      if (!tools) return;
      const rect = selectionRect(range);
      const paragraphBox = paragraph.getBoundingClientRect();
      const left = Math.max(4, Math.min(rect.right - paragraphBox.left + 8, paragraphBox.width - 32));
      const top = Math.max(4, rect.top - paragraphBox.top - 2);
      tools.style.left = left + "px";
      tools.style.right = "auto";
      tools.style.top = top + "px";
    }

    // ---- Comment-on-highlight: reveal an icon-only comment button while text
    // is selected inside one rendered document paragraph. ----
    document.addEventListener("selectionchange", function () {
      clearSelectionCommentAffordances();
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return;
      const range = sel.getRangeAt(0);
      const anchor = sel.anchorNode;
      const el = anchor && anchor.nodeType === 3 ? anchor.parentElement : anchor;
      const para = el && el.closest ? el.closest("#reviewView .studio-doc-paragraph") : null;
      if (para && para.contains(sel.focusNode)) {
        para.classList.add("has-selection");
        positionSelectionCommentButton(para, range);
      }
    });
    document.addEventListener("focusin", function (event) {
      if (!event.target.closest || event.target.closest("#reviewView .studio-page")) return;
      clearSelectionCommentAffordances();
    });
    // Pressing the comment button must not clear the text selection.
    document.addEventListener("mousedown", function (event) {
      if (event.target.closest && event.target.closest(".paragraph-comment-add")) {
        event.preventDefault();
      }
    });

    applyZoom();
    updatePages();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initViewerControls);
  } else {
    initViewerControls();
  }
})();
