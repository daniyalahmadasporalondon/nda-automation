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
} = {}) {
  // POSTs the buildDraftPayload output and normalises the response. Throws
  // GenerationUnavailableError on 404 (degrade) and a plain Error with the
  // backend message on any other failure.
  async function generateNda(payload) {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      },
      body: JSON.stringify(payload),
    });

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
