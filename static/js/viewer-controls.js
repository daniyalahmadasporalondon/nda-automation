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
    const ZOOM_MIN = 70;
    const ZOOM_MAX = 150;
    const ZOOM_STEP = 10;
    const ZOOM_DEFAULT = 90;
    const PAGE_BASE_WIDTH = 938;
    const PAGE_BASE_PADDING_Y = 34;
    const PAGE_BASE_PADDING_X = 30;
    const PAGE_BASE_FONT_SIZE = 15;
    const PAGE_BASE_GAP = 10;
    const PAGE_BASE_SUBTITLE_MARGIN = 16;
    const PAGE_BASE_SUBTITLE_PADDING = 10;
    const PAGE_BASE_PARAGRAPH_PADDING_Y = 7;
    const PAGE_BASE_PARAGRAPH_PADDING_X = 10;
    let zoom = ZOOM_DEFAULT;
    function scaledPx(value) {
      return (value * zoom) / 100 + "px";
    }
    function applyZoom() {
      pageEl.style.setProperty("--review-page-width", scaledPx(PAGE_BASE_WIDTH));
      pageEl.style.setProperty("--review-page-padding-y", scaledPx(PAGE_BASE_PADDING_Y));
      pageEl.style.setProperty("--review-page-padding-x", scaledPx(PAGE_BASE_PADDING_X));
      pageEl.style.setProperty("--review-page-font-size", scaledPx(PAGE_BASE_FONT_SIZE));
      pageEl.style.setProperty("--review-page-gap", scaledPx(PAGE_BASE_GAP));
      pageEl.style.setProperty("--review-page-subtitle-margin", scaledPx(PAGE_BASE_SUBTITLE_MARGIN));
      pageEl.style.setProperty("--review-page-subtitle-padding", scaledPx(PAGE_BASE_SUBTITLE_PADDING));
      pageEl.style.setProperty("--review-page-paragraph-padding-y", scaledPx(PAGE_BASE_PARAGRAPH_PADDING_Y));
      pageEl.style.setProperty("--review-page-paragraph-padding-x", scaledPx(PAGE_BASE_PARAGRAPH_PADDING_X));
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

    // ---- Page scroll ----
    // FIX 2 (P1): the indicator used to read ceil(scrollHeight / viewportHeight) --
    // a count of viewport-sized SCROLL slices over the reconstructed text, NOT the
    // document's real page count. For an image-rendered matter the document renders
    // as N discrete page-image tiles (figure.review-render-page); the slice count
    // could read "17" for a 7-page PDF purely because the reconstructed text was
    // taller than 7 viewports. So when real page tiles are present we report the
    // TILE count (the true page count) and the current page is the tile whose top is
    // nearest the top of the viewport; Next/Prev then step tile-to-tile. When there
    // are no tiles (DOCX / faithful reconstruction = genuine continuous scroll) we
    // keep the original viewport-slice pagination unchanged.
    function pageTiles() {
      return Array.prototype.slice.call(
        scrollEl.querySelectorAll("[data-review-render-page]"),
      );
    }
    function tileMetrics(tiles) {
      const scrollRect = scrollEl.getBoundingClientRect();
      const total = tiles.length;
      // Current = the last tile whose top edge is at or above the viewport top
      // (i.e. the page currently filling the top of the pane), defaulting to 1.
      let current = 1;
      for (let i = 0; i < tiles.length; i += 1) {
        const top = tiles[i].getBoundingClientRect().top - scrollRect.top;
        // 4px slack so a tile resting flush at the top still counts as "reached".
        if (top <= 4) current = i + 1;
        else break;
      }
      return { total: total, current: Math.min(total, Math.max(1, current)) };
    }
    function pageMetrics() {
      const tiles = pageTiles();
      if (tiles.length) {
        const m = tileMetrics(tiles);
        return { tiles: tiles, total: m.total, current: m.current };
      }
      const vh = scrollEl.clientHeight || 1;
      const total = Math.max(1, Math.ceil(scrollEl.scrollHeight / vh));
      const current = Math.min(total, Math.floor(scrollEl.scrollTop / vh) + 1);
      return { tiles: null, vh: vh, total: total, current: current };
    }
    function scrollToTile(tile) {
      if (!tile) return;
      const scrollRect = scrollEl.getBoundingClientRect();
      const top = tile.getBoundingClientRect().top - scrollRect.top + scrollEl.scrollTop;
      scrollEl.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    }
    function stepPage(delta) {
      const m = pageMetrics();
      if (m.tiles && m.tiles.length) {
        const targetIndex = Math.min(m.total - 1, Math.max(0, m.current - 1 + delta));
        scrollToTile(m.tiles[targetIndex]);
        return;
      }
      // Continuous-scroll (no tiles): keep the ~90%-of-a-viewport nudge.
      scrollEl.scrollBy({ top: delta * Math.round(scrollEl.clientHeight * 0.9), behavior: "smooth" });
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
        stepPage(-1);
      });
    }
    if (pageNext) {
      pageNext.addEventListener("click", function () {
        stepPage(1);
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
