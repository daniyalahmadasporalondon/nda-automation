// Faithful DOCX render path for the Review workstation surfaces.
//
// PROBLEM this solves: for a DOCX-source matter we already hold the real .docx
// bytes, but the workstation never renders them -- the structured/redline view is
// a hand-built reconstruction from extracted text/paragraphs, so styles, tables,
// numbering and tracked changes are approximated. This module renders the ACTUAL
// .docx (including w:ins / w:del tracked changes) using the locally-vendored
// docx-preview library, so the surface is byte-faithful.
//
// SCOPE & SAFETY CONTRACT (read before editing):
//   * Default ON behind a feature flag (faithfulDocxRenderEnabled()). SINGLE
//     control path: localStorage["nda.faithfulDocxRender"]; only the explicit value
//     "false" disables it (the kill-switch), every other value / absent key enables
//     (persists across reloads; see static/js/config.js). There is NO window flag.
//   * REUSABLE: renderFaithfulDocx(container, { bytes | url }) takes DOCX bytes
//     (or a same-origin URL to fetch them) + a container element. It is NOT
//     hardwired to one matter type, so a later PDF->canonical-DOCX effort can feed
//     a canonical DOCX built from a PDF source through the SAME function.
//   * NEVER BLANK: every failure path (flag off, library missing, no bytes,
//     fetch/parse throws, or an empty container after render) resolves to a
//     non-fatal { ok:false, reason } result -- it NEVER throws -- so the caller
//     falls back to the existing renderer. A bad faithful render must DEGRADE to
//     the reconstruction, never blank the pane.
//   * The vendored libs are LAZILY self-injected (loadScriptOnce/
//     ensureFaithfulDocxLibs) on first render, with retry-on-failure: we do NOT
//     rely on static <script> tags in index.html that would silently report
//     library_unavailable forever if a tag 404'd. Nothing ever leaves the browser.
//   * It only ever paints into the container the caller hands it; it does not
//     touch the structured/redline view, the overview panel or insert-into-blanks.

// ---------------------------------------------------------------------------
// Feature flag: localStorage-backed, default ON.
// ---------------------------------------------------------------------------
// The single control path is localStorage["nda.faithfulDocxRender"]. config.js
// defines FAITHFUL_DOCX_RENDER_FLAG_KEY / FAITHFUL_DOCX_RENDER_DEFAULT; we read
// them off window when present and fall back to literals so this module also
// works in isolation (e.g. the headless vm test that loads only this file).
// The flag now defaults ON: only an explicit "false" disables it (the kill-switch).
const FAITHFUL_DOCX_FLAG_KEY_FALLBACK = "nda.faithfulDocxRender";

function faithfulDocxFlagKey() {
  if (typeof FAITHFUL_DOCX_RENDER_FLAG_KEY === "string" && FAITHFUL_DOCX_RENDER_FLAG_KEY) {
    return FAITHFUL_DOCX_RENDER_FLAG_KEY;
  }
  if (typeof window !== "undefined" && typeof window.FAITHFUL_DOCX_RENDER_FLAG_KEY === "string") {
    return window.FAITHFUL_DOCX_RENDER_FLAG_KEY;
  }
  return FAITHFUL_DOCX_FLAG_KEY_FALLBACK;
}

function faithfulDocxFlagDefault() {
  if (typeof FAITHFUL_DOCX_RENDER_DEFAULT === "boolean") return FAITHFUL_DOCX_RENDER_DEFAULT;
  if (typeof window !== "undefined" && typeof window.FAITHFUL_DOCX_RENDER_DEFAULT === "boolean") {
    return window.FAITHFUL_DOCX_RENDER_DEFAULT;
  }
  return true;
}

// Default ON: enabled unless the localStorage flag is explicitly "false" (the
// ops/user kill-switch). Absent key -> the config default (ON). Any localStorage
// access error -> default, so a hardened/incognito context never throws. The only
// value that DISABLES is the literal "false" (case/space-insensitive); every other
// value (incl. the legacy "1"/"true"/"on"/"yes" and an absent key) is ENABLED. Kept a
// function (not a const) so a late flip via localStorage takes effect on the next
// render without a reload.
function faithfulDocxRenderEnabled() {
  let raw = null;
  try {
    if (typeof localStorage !== "undefined" && localStorage) {
      raw = localStorage.getItem(faithfulDocxFlagKey());
    } else if (typeof window !== "undefined" && window.localStorage) {
      raw = window.localStorage.getItem(faithfulDocxFlagKey());
    }
  } catch (_error) {
    return faithfulDocxFlagDefault();
  }
  if (raw == null) return faithfulDocxFlagDefault();
  return String(raw).trim().toLowerCase() !== "false";
}

// ---------------------------------------------------------------------------
// Lazy vendored-library loader (no CDN; confidential docs never leave the browser).
// ---------------------------------------------------------------------------
// docx-preview is a UMD bundle that reads a global JSZip, so jszip MUST load first.
// We self-inject the vendored <script>s on first use rather than depending on
// static tags in index.html: a static tag that 404s would leave the library
// permanently "unavailable" with no recovery. The promise cache is RESET on
// failure so a transient blip can be retried on the next render.
const FAITHFUL_DOCX_VENDOR = {
  jszip: "/static/vendor/jszip/jszip.min.js",
  docxPreview: "/static/vendor/docx-preview/docx-preview.min.js",
};

let faithfulDocxLibsPromise = null;

function loadScriptOnce(src) {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error("No document available to load script"));
      return;
    }
    const existing = document.querySelector(`script[data-faithful-docx-src="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === "1") {
        resolve();
        return;
      }
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error(`Failed to load ${src}`)), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = false; // preserve load order (jszip before docx-preview)
    script.dataset.faithfulDocxSrc = src;
    script.addEventListener("load", () => {
      script.dataset.loaded = "1";
      resolve();
    }, { once: true });
    script.addEventListener("error", () => reject(new Error(`Failed to load ${src}`)), { once: true });
    document.head.appendChild(script);
  });
}

// Resolves to the docx-preview global (window.docx). Already-present globals short
// out the injection. On any failure the cache is cleared so the next call retries.
function ensureFaithfulDocxLibs() {
  if (faithfulDocxLibsPromise) return faithfulDocxLibsPromise;
  faithfulDocxLibsPromise = (async () => {
    if (faithfulDocxLibraryAvailable()) return window.docx;
    await loadScriptOnce(FAITHFUL_DOCX_VENDOR.jszip);
    await loadScriptOnce(FAITHFUL_DOCX_VENDOR.docxPreview);
    if (!faithfulDocxLibraryAvailable()) {
      throw new Error("docx-preview did not initialise (window.docx.renderAsync missing)");
    }
    return window.docx;
  })().catch((error) => {
    // Reset so a transient failure (offline blip / 404) can be retried on the next
    // render rather than poisoning the cache forever.
    faithfulDocxLibsPromise = null;
    throw error;
  });
  return faithfulDocxLibsPromise;
}

// True only when BOTH vendored globals are present and usable. Used as a cheap
// synchronous capability probe by the caller's selection function. (When false the
// caller's plan stays page_image; the actual render still lazy-loads via
// ensureFaithfulDocxLibs, so a not-yet-injected library is not a permanent "no".)
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

// True when, after a render attempt, the container holds VISIBLE document content.
//
// IMPORTANT: docx-preview's styleContainer defaults to the SAME node it renders
// into, so it injects a <style> element there even for an empty-body DOCX. Naively
// measuring container.textContent therefore counts the CSS text and a CSS-only
// (empty body) render would falsely pass -- blanking the user's content. So we
// require a real rendered element (a .docx / section / .faithful-docx node), and
// when measuring text we EXCLUDE <style>/<script> nodes.
function faithfulDocxContainerHasContent(container) {
  if (!container) return false;
  // A genuine docx-preview render emits a top-level document node. Its presence is
  // the reliable signal that real content (not just an injected <style>) landed.
  if (typeof container.querySelector === "function") {
    if (container.querySelector(".docx, section, article, .faithful-docx")) return true;
  }
  return faithfulDocxVisibleTextLength(container) > 0;
}

// Length of the container's text EXCLUDING <style>/<script> nodes, so injected CSS
// never counts as "content". Falls back to raw textContent only when DOM traversal
// is unavailable (e.g. a bare stub element in a unit test).
function faithfulDocxVisibleTextLength(container) {
  if (!container) return 0;
  const children = container.children;
  if (children && typeof children.length === "number" && typeof container.querySelectorAll === "function") {
    let length = 0;
    for (let index = 0; index < children.length; index += 1) {
      const node = children[index];
      const tag = node && node.tagName ? String(node.tagName).toUpperCase() : "";
      if (tag === "STYLE" || tag === "SCRIPT") continue;
      length += String(node.textContent || "").trim().length;
    }
    return length;
  }
  return String(container.textContent || "").trim().length;
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

  let lib = null;
  try {
    lib = await ensureFaithfulDocxLibs();
  } catch (error) {
    return { ok: false, reason: "library_unavailable", error };
  }
  if (!lib || typeof lib.renderAsync !== "function") {
    return { ok: false, reason: "library_unavailable" };
  }

  let data = faithfulDocxNormalizeBytes(source && source.bytes);
  if (!data && source && source.url) {
    data = faithfulDocxNormalizeBytes(await faithfulDocxFetchBytes(source.url));
  }
  if (!data) return { ok: false, reason: "no_bytes" };

  // Pass a SEPARATE styleContainer so docx-preview's injected <style> never lands
  // in the measured render node. We render into a detached scratch host first and
  // only adopt it if it produced real content, so a failed/empty render can never
  // blank the live container.
  const ownerDoc = (typeof container.ownerDocument !== "undefined" && container.ownerDocument)
    || (typeof document !== "undefined" ? document : null);
  let scratch = container;
  let styleHost = null;
  const canDetach = ownerDoc && typeof ownerDoc.createElement === "function";
  if (canDetach) {
    scratch = ownerDoc.createElement("div");
    scratch.className = "faithful-docx-host";
    styleHost = ownerDoc.createElement("div");
    styleHost.className = "faithful-docx-style-host";
  }

  try {
    if (!canDetach) {
      // No document to build a detached host: render straight into the container.
      container.innerHTML = "";
    }
    await lib.renderAsync(
      data,
      scratch,
      styleHost || scratch,
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

  if (!faithfulDocxContainerHasContent(scratch)) {
    return { ok: false, reason: "empty_render" };
  }

  if (canDetach && scratch !== container) {
    // Adopt the proven-non-empty render (plus its style host) into the live node.
    container.innerHTML = "";
    if (styleHost) container.appendChild(styleHost);
    container.appendChild(scratch);
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
    ensureLibs: ensureFaithfulDocxLibs,
    render: renderFaithfulDocx,
  };
}

// Also export for the headless test (Node ESM import via createRequire/vm is
// avoided; the test loads this file's functions through a small shim).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    ensureFaithfulDocxLibs,
    faithfulDocxContainerHasContent,
    faithfulDocxNormalizeBytes,
    faithfulDocxRenderEnabled,
    faithfulDocxRenderOptions,
    faithfulDocxVisibleTextLength,
    renderFaithfulDocx,
  };
}
