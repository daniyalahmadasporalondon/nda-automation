// NDA generation API wrapper for the outbound-draft "Generate NDA" flow.
//
// The draft-intake controller captures the entity bundle + intake into the
// buildDraftPayload shape (static/js/modules/draft-intake.mjs); this wrapper is
// the seam that POSTs that payload to the generation endpoint and normalises the
// response so the controller can either trigger a DOCX download or show a saved
// artifact, without caring how the backend chose to return it.
//
// The endpoint (POST /api/generate-nda) lives on the generation branch and is
// not deployed on this base yet — exactly like /api/signing-entities for the
// entity picker. So a 404 is treated as "not available yet" (a distinct,
// non-error signal the controller degrades on) rather than a hard failure, which
// keeps the form usable until integration wires the real route.
//
// The endpoint (confirmed against nda_automation/routes/generation.py) returns a
// 201 JSON body: { matter_id, artifact_id, status:"generated", download_url:
// "/api/matters/<id>/source", self_check:{passed, overall_status, ...}, manifest }.
// It also accepts the buildDraftPayload shape verbatim (it tolerates both the
// nested {signing_entity:{id}, counterparty:{name}, project_purpose, term, ...}
// shape draft-ui emits and a flatter one), so the FE sends the payload as-is.
//
// Two response modes are normalised so the FE never has to be rebuilt if
// generation later switches to streaming the bytes directly:
//   - JSON  (the real contract): returned as { kind: "json", ...payload }. The
//     controller shows a saved state and downloads via download_url.
//   - DOCX BYTES (Content-Type a Word doc, like /api/export-review-docx):
//     returned as { kind: "blob", blob, filename }. The controller downloads it.

const DOCX_CONTENT_TYPES = [
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/msword",
  "application/octet-stream",
];

// Raised when the endpoint is not deployed on this base (404). The controller
// catches this by `code` to show the same "pending" copy the stub showed,
// rather than surfacing it as a generation failure.
export class GenerationUnavailableError extends Error {
  constructor(message = "NDA generation is not available yet.") {
    super(message);
    this.name = "GenerationUnavailableError";
    this.code = "generation_unavailable";
  }
}

// Raised when the POST exceeds the client timeout (or the connection is aborted
// before a response). Generation is synchronous and, when the AI clause adapter
// is active, makes several live model calls — on a cold/slow host the request can
// outlast a sane client wait (or a proxy can hold the socket open after the
// backend already finished and SAVED the matter). The controller catches this by
// `code` to SELF-HEAL: poll the repository for the matter the backend may have
// created, instead of spinning on "Generating…" forever. Distinct from a hard
// failure so a real 4xx/5xx still surfaces as an error.
export class GenerationTimeoutError extends Error {
  constructor(message = "NDA generation timed out.") {
    super(message);
    this.name = "GenerationTimeoutError";
    this.code = "generation_timeout";
  }
}

// Default client-side ceiling for the synchronous generate POST. Comfortably
// above a normal AI-adapted generation (~10–15s warm) yet bounded so a hung
// request/proxy can't pin the spinner — past this the controller switches to the
// repository self-heal. Overridable via createGenerationApi({ timeoutMs }).
export const DEFAULT_GENERATE_TIMEOUT_MS = 45000;

function isDocxResponse(response) {
  const contentType = (response.headers?.get?.("Content-Type") || "").toLowerCase();
  return DOCX_CONTENT_TYPES.some((type) => contentType.includes(type));
}

// Pulls the filename from Content-Disposition, falling back to a safe default so
// the browser download is always named even if the header is absent.
function filenameFromResponse(response, fallback = "nda.docx") {
  const disposition = response.headers?.get?.("Content-Disposition") || "";
  const match = disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)"?/i);
  const name = match ? decodeURIComponent(match[1].trim()) : "";
  return name || fallback;
}

// Best-effort extraction of a human-readable error from a failed response. The
// endpoint returns JSON {error|message|detail} like the other authed routes; if
// the body is not JSON we fall back to the supplied default.
async function errorMessageFromResponse(response, fallback) {
  try {
    const payload = await response.json();
    return payload?.error || payload?.message || payload?.detail || fallback;
  } catch (error) {
    return fallback;
  }
}

export function createGenerationApi({
  fetchImpl = globalThis.fetch,
  url = "/api/generate-nda",
  // Client timeout for the synchronous generate POST. <= 0 disables it (the old
  // unbounded behaviour, kept for tests that drive a controlled fetchImpl).
  timeoutMs = DEFAULT_GENERATE_TIMEOUT_MS,
} = {}) {
  // POSTs the buildDraftPayload output and normalises the response. Throws
  // GenerationUnavailableError on 404 (degrade), GenerationTimeoutError when the
  // request outlasts `timeoutMs` or is aborted (the controller self-heals from
  // the repository), and a plain Error with the backend message on any other
  // failure.
  async function generateNda(payload) {
    // AbortController bounds the wait: generation is synchronous and the AI
    // adapter adds live model calls, so without this a stalled socket (or a proxy
    // holding the connection after the backend already saved the matter) would
    // leave the caller awaiting forever — the exact "spinner stuck, NDA in repo"
    // failure. On timeout we abort and raise GenerationTimeoutError so the caller
    // can recover rather than hang. Guard for environments/tests without
    // AbortController.
    const canAbort = typeof AbortController === "function" && timeoutMs > 0;
    const controller = canAbort ? new AbortController() : null;
    let timedOut = false;
    let timer = null;
    if (controller) {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
    }

    let response;
    try {
      response = await fetchImpl(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
        body: JSON.stringify(payload),
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (error) {
      // An abort (our timeout) OR a network drop mid-flight both land here. Both
      // can leave a backend-completed-but-unacknowledged generation, so both map
      // to the recoverable timeout signal rather than a hard error.
      if (timedOut || error?.name === "AbortError") {
        throw new GenerationTimeoutError();
      }
      throw error;
    } finally {
      if (timer !== null) clearTimeout(timer);
    }

    if (response.status === 404) {
      throw new GenerationUnavailableError();
    }
    if (!response.ok) {
      throw new Error(await errorMessageFromResponse(response, "Could not generate the NDA."));
    }

    if (isDocxResponse(response)) {
      const blob = await response.blob();
      return { kind: "blob", blob, filename: filenameFromResponse(response) };
    }

    const data = await response.json();
    return { kind: "json", ...data };
  }

  return { generateNda };
}
