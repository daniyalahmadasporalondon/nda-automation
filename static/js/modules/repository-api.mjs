export function createRepositoryApi({ fetchImpl = globalThis.fetch, reviewErrorFromPayload }) {
  // Check `response.ok` BEFORE parsing JSON. On session expiry the server returns
  // a 401 with an empty/HTML body, so `response.json()` itself throws a SyntaxError
  // ("The string did not match the expected pattern." in Safari) that masks the
  // real cause. Only parse when ok; on a non-ok response throw a clean Error
  // carrying the real `status`, and fire the global auth-expired prompt on a 401.
  async function jsonRequest(url, options = {}, fallbackMessage = "Request failed") {
    const response = await fetchImpl(url, options);
    if (response.ok) return response.json();
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const error = reviewErrorFromPayload(payload, fallbackMessage);
    error.status = response.status;
    if (response.status === 401) globalThis.AuthExpired?.handleAuthExpired?.();
    throw error;
  }

  async function loadGmailStatus() {
    const payload = await jsonRequest("/api/gmail/status", {}, "Gmail status could not load");
    return payload.gmail || {};
  }

  async function syncGmail({ limit = 25 } = {}) {
    const payload = await jsonRequest(
      "/api/gmail/import",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit }),
      },
      "Gmail sync could not run",
    );
    return payload;
  }

  async function listMatters() {
    const payload = await jsonRequest("/api/matters", {}, "Repository could not load");
    return Array.isArray(payload.matters) ? payload.matters : [];
  }

  async function getMatter(matterId) {
    const payload = await jsonRequest(
      `/api/matters/${encodeURIComponent(matterId)}`,
      {},
      "Matter could not load",
    );
    return payload.matter;
  }

  async function deleteMatter(matterId) {
    return jsonRequest(
      `/api/matters/${encodeURIComponent(matterId)}`,
      { method: "DELETE" },
      "Matter could not be deleted",
    );
  }

  async function getMatterReview(matterId, options = {}) {
    const refresh = options?.refresh === true;
    const payload = await jsonRequest(
      refresh
        ? `/api/matters/${encodeURIComponent(matterId)}/review-refresh`
        : `/api/matters/${encodeURIComponent(matterId)}/review`,
      refresh ? { method: "POST" } : {},
      "Matter review details could not load",
    );
    // `review_may_be_stale` may arrive at the payload top level or on the matter
    // object. Opening a matter sets it WITHOUT running the AI review; the explicit
    // refresh path clears it. Read it defensively from either location.
    const mayBeStale = Boolean(
      payload.review_may_be_stale ?? payload.matter?.review_may_be_stale,
    );
    const reviewMatter = {
      ...(payload.matter || {}),
      extracted_text: payload.extracted_text || "",
      redline_draft: payload.redline_draft || null,
      review_may_be_stale: mayBeStale,
      review_refresh: payload.review_refresh || null,
      review_result: payload.review_result || {},
    };
    const renderMetadata = payload.document_render || payload.rendered_document || payload.pdf_render || null;
    if (renderMetadata) reviewMatter.document_render = renderMetadata;
    return reviewMatter;
  }

  async function moveMatterToColumn(matterId, boardColumn) {
    const payload = await jsonRequest(
      `/api/matters/${encodeURIComponent(matterId)}/stage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ board_column: boardColumn }),
      },
      "Matter could not move",
    );
    if (!payload.matter?.id) throw new Error("Matter could not move");
    return payload.matter;
  }

  async function exportReviewDocx(matterId) {
    const response = await fetchImpl("/api/export-review-docx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ matter_id: matterId }),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw reviewErrorFromPayload(payload, "Export could not run");
    }
    return response;
  }

  async function sendRedline(sendPayload) {
    return jsonRequest(
      "/api/gmail/send-redline",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sendPayload),
      },
      "Redline email could not send",
    );
  }

  async function driveStatus() {
    const payload = await jsonRequest("/api/drive/status", {}, "Drive status could not load");
    return payload || {};
  }

  // Upload the matter's NDA to Google Drive. Unlike the other helpers this does
  // not throw on a 409: the backend signals "not connected" with a structured
  // body (needs_connect + connect_url) that the caller turns into a Connect
  // prompt, so we hand the raw status + payload back instead of an Error.
  async function saveMatterToDrive(matterId) {
    const response = await fetchImpl("/api/drive/upload-matter", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ matter_id: matterId }),
    });
    const payload = await response.json();
    return { ok: response.ok, status: response.status, payload };
  }

  return {
    deleteMatter,
    driveStatus,
    exportReviewDocx,
    getMatter,
    getMatterReview,
    listMatters,
    loadGmailStatus,
    moveMatterToColumn,
    saveMatterToDrive,
    sendRedline,
    syncGmail,
  };
}
