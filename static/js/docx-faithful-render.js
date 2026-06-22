// Faithful DOCX render path for the Review workstation "Original" surface.
//
// PROBLEM this solves: for a DOCX-source matter we already hold the real .docx
// bytes, but the workstation never renders them -- the structured/redline view is
// a hand-built reconstruction from extracted text/paragraphs, so styles, tables,
// numbering and tracked changes are approximated. This module renders the ACTUAL
// .docx (including w:ins / w:del tracked changes) using the locally-vendored
// docx-preview library, so the "Original" view is byte-faithful.
//
// SCOPE & SAFETY CONTRACT (read before editing):
//   * Default OFF behind a feature flag (faithfulDocxRenderEnabled()). When off,
//     this module renders nothing and the caller keeps the existing surface.
//   * REUSABLE: renderFaithfulDocx(container, { bytes | url }) takes DOCX bytes
//     (or a same-origin URL to fetch them) + a container element. It is NOT
//     hardwired to one matter type, so a later Approach-C effort can feed a
//     canonical DOCX built from a PDF source through the SAME function.
//   * NEVER BLANK: every failure path (flag off, library missing, no bytes,
//     fetch/parse throws, or an empty container after render) resolves to a
//     non-fatal result so the caller falls back to the existing renderer. A bad
//     faithful render must DEGRADE to the reconstruction, never blank the pane.
//   * It only ever paints into the container the caller hands it; it does not
//     touch the structured/redline view, the overview panel or insert-into-blanks.

// Feature flag: default OFF. Flip it on in a running app by setting
//   window.NDA_FAITHFUL_DOCX_RENDER = true
// (e.g. from the browser console / a preview harness) and re-opening / re-rendering
// the matter. Truthy values "1"/"true"/"on"/"yes" are also accepted so it can be
// driven from a bootstrap string. Kept as a function (not a const) so a late flip
// takes effect without a reload.
function faithfulDocxRenderEnabled() {
  if (typeof window === "undefined") return false;
  const value = window.NDA_FAITHFUL_DOCX_RENDER;
  if (value === true) return true;
  if (typeof value === "string") {
    return ["1", "true", "on", "yes"].includes(value.trim().toLowerCase());
  }
  return false;
}

// True only when BOTH vendored globals are present. docx-preview (UMD) reads the
// global JSZip and exposes window.docx.renderAsync; if either script failed to
// load we must fall back rather than throw.
function faithfulDocxLibraryAvailable() {
  return Boolean(
    typeof window !== "undefined"
    && window.docx
    && typeof window.docx.renderAsync === "function"
    && window.JSZip,
  );
}

// Options passed to docx-preview. renderChanges:true is the VALIDATED option that
// makes Word tracked changes render (w:ins -> <ins>, w:del -> <del>); it defaults
// to false in the library, so it MUST be set. inWrapper:false drops the library's
// gray page-chrome wrapper so the render sits cleanly inside our own surface.
function faithfulDocxRenderOptions(overrides) {
  return {
    breakPages: true,
    ignoreLastRenderedPageBreak: false,
    inWrapper: false,
    renderChanges: true,
    renderComments: false,
    renderFooters: true,
    renderHeaders: true,
    ...(overrides && typeof overrides === "object" ? overrides : {}),
  };
}

// Coerces input bytes into a value docx-preview/JSZip accepts. A Blob, ArrayBuffer
// or ArrayBufferView (Uint8Array) all pass straight through; anything else is
// rejected (returns null) so the caller falls back.
function faithfulDocxNormalizeBytes(bytes) {
  if (!bytes) return null;
  if (typeof Blob !== "undefined" && bytes instanceof Blob) return bytes;
  if (bytes instanceof ArrayBuffer) return bytes;
  if (ArrayBuffer.isView(bytes)) return bytes;
  return null;
}

// Fetches the DOCX bytes from a SAME-ORIGIN url as a Blob. Returns null on any
// non-OK response / network error so the caller degrades. The url is expected to
// be one of our own owner-scoped endpoints (e.g. /api/matters/<id>/source); we do
// not add a new endpoint here.
async function faithfulDocxFetchBytes(url) {
  if (typeof fetch !== "function" || !url) return null;
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response || !response.ok) return null;
    return await response.blob();
  } catch (_error) {
    return null;
  }
}

// True when, after a render attempt, the container actually has visible content.
// docx-preview can resolve "successfully" yet leave the container effectively
// empty for a malformed part; an empty container must trigger fallback, not a
// blank pane.
function faithfulDocxContainerHasContent(container) {
  if (!container) return false;
  if (container.childElementCount > 0) return true;
  return String(container.textContent || "").trim().length > 0;
}

// Render the real DOCX faithfully into `container`. Returns a result object:
//   { ok: true }                              -> faithful render painted, use it
//   { ok: false, reason, error? }             -> caller MUST fall back
// Accepts either { bytes } (Blob/ArrayBuffer/Uint8Array) or { url } (same-origin).
// Never throws: every failure is reported as { ok:false } so the caller can
// degrade to the existing renderer.
async function renderFaithfulDocx(container, source, options) {
  if (!container) return { ok: false, reason: "no_container" };
  if (!faithfulDocxRenderEnabled()) return { ok: false, reason: "flag_off" };
  if (!faithfulDocxLibraryAvailable()) return { ok: false, reason: "library_unavailable" };

  let data = faithfulDocxNormalizeBytes(source && source.bytes);
  if (!data && source && source.url) {
    data = faithfulDocxNormalizeBytes(await faithfulDocxFetchBytes(source.url));
  }
  if (!data) return { ok: false, reason: "no_bytes" };

  try {
    // Clear any prior content so a previous render / skeleton never bleeds through
    // a partial paint.
    container.innerHTML = "";
    await window.docx.renderAsync(
      data,
      container,
      null,
      faithfulDocxRenderOptions(options),
    );
  } catch (error) {
    try {
      // eslint-disable-next-line no-console
      console.error("renderFaithfulDocx: docx-preview render failed; degrading to reconstruction", error);
    } catch (_loggingError) {
      // never let a logging failure swallow the fallback signal
    }
    return { ok: false, reason: "render_threw", error };
  }

  if (!faithfulDocxContainerHasContent(container)) {
    return { ok: false, reason: "empty_render" };
  }
  return { ok: true };
}

// Expose on window so the rendering module (a classic, non-module script) can call
// in, mirroring how the rest of the workstation bridges helpers. Idempotent so a
// reload never clobbers a live reference.
if (typeof window !== "undefined") {
  window.FaithfulDocxRender = window.FaithfulDocxRender || {
    enabled: faithfulDocxRenderEnabled,
    libraryAvailable: faithfulDocxLibraryAvailable,
    render: renderFaithfulDocx,
  };
}

// Also export for the headless test (Node ESM import via createRequire/vm is
// avoided; the test loads this file's functions through a small shim).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    faithfulDocxContainerHasContent,
    faithfulDocxNormalizeBytes,
    faithfulDocxRenderEnabled,
    faithfulDocxRenderOptions,
    renderFaithfulDocx,
  };
}
