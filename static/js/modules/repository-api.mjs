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

  // Like jsonRequest, but hands back the raw HTTP `status` alongside the parsed
  // body so a caller can distinguish a 202 (async work scheduled) from a 200. The
  // 401/auth-expired + non-ok error contract is identical to jsonRequest; only the
  // success return differs (here a 2xx returns { status, payload } instead of the
  // bare body). Used ONLY by the refresh path below so jsonRequest's single-return
  // contract — which every other caller (listMatters/getMatter/…) relies on —
  // stays untouched.
  async function jsonRequestWithStatus(url, options = {}, fallbackMessage = "Request failed") {
    const response = await fetchImpl(url, options);
    if (response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      return { status: response.status, payload: payload || {} };
    }
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

  async function getMatterReview(matterId, options = {}) {
    const refresh = options?.refresh === true;

    // OPEN path (refresh:false → GET /review): the stored review is returned
    // synchronously (200, never 202). Keep this byte-identical to the prior
    // behavior — it has no concept of in-progress and downstream callers consume a
    // full result object. Do NOT add the in-progress sentinel here.
    if (!refresh) {
      const payload = await jsonRequest(
        `/api/matters/${encodeURIComponent(matterId)}/review`,
        {},
        "Matter review details could not load",
      );
      return buildReviewMatter(payload);
    }

    // REFRESH path (refresh:true → POST /review-refresh): the AI review now runs
    // ASYNCHRONOUSLY. The server returns 202 with the matter stamped
    // review_status:"in_progress" while a worker does the heavy review; the body
    // carries NO review_result yet. Detect that explicitly and return an
    // in-progress SENTINEL — { inProgress: true, matter } — so the caller starts
    // polling instead of injecting a misleading BLANK review_result:{} into the
    // panel (the old bug). A 200 (e.g. idle: nothing to re-run) flows through to
    // the normal full-result shape exactly as before.
    const { status, payload } = await jsonRequestWithStatus(
      `/api/matters/${encodeURIComponent(matterId)}/review-refresh`,
      { method: "POST" },
      "Matter review details could not load",
    );
    const inProgress = status === 202
      || String(payload.review_status || payload.matter?.review_status || "") === "in_progress";
    if (inProgress) {
      // Carry the matter (with its review_status:"in_progress") for the board badge,
      // but deliberately do NOT fabricate review_result — there is no result yet.
      return {
        inProgress: true,
        matter: { ...(payload.matter || {}), id: payload.matter?.id ?? matterId },
        review_refresh: payload.review_refresh || null,
      };
    }
    return buildReviewMatter(payload);
  }

  // Shape the full review payload (200/idle) into the matter object the panel +
  // Review tab consume. Shared by the open path and the refresh path's non-202
  // (idle) branch so both render results identically.
  function buildReviewMatter(payload) {
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
