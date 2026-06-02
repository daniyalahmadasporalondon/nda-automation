export function createRepositoryApi({ fetchImpl = globalThis.fetch, reviewErrorFromPayload }) {
  async function jsonRequest(url, options = {}, fallbackMessage = "Request failed") {
    const response = await fetchImpl(url, options);
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, fallbackMessage);
    return payload;
  }

  async function loadGmailStatus() {
    const payload = await jsonRequest("/api/gmail/status", {}, "Gmail status could not load");
    return payload.gmail || {};
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

  async function getMatterReview(matterId) {
    const payload = await jsonRequest(
      `/api/matters/${encodeURIComponent(matterId)}/review`,
      {},
      "Matter review details could not load",
    );
    return {
      ...(payload.matter || {}),
      extracted_text: payload.extracted_text || "",
      redline_draft: payload.redline_draft || null,
      review_result: payload.review_result || {},
    };
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

  return {
    deleteMatter,
    exportReviewDocx,
    getMatter,
    getMatterReview,
    listMatters,
    loadGmailStatus,
    moveMatterToColumn,
    sendRedline,
  };
}
