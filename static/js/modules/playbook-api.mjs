// Thin fetch wrapper for the Playbook draft/publish/validate flow.
//
// All endpoint paths live here so that, once the backend contract is final, any
// path/payload change is a one-file edit. The controller consumes the returned
// JSON and hands it to the pure helpers in playbook-draft.mjs.

export function createPlaybookApi({ fetchImpl = globalThis.fetch } = {}) {
  async function jsonRequest(url, options = {}, fallbackMessage = "Request failed") {
    const response = await fetchImpl(url, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(payload.error || payload.message || fallbackMessage);
    }
    return payload;
  }

  function jsonBody(body) {
    return {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  // Build the opt-in optimistic-concurrency hints the backend honors. Derived
  // from the active block's metadata so save/publish fail with 409 if another
  // editor published in the meantime, instead of silently clobbering.
  function expectedActiveFields(activeMeta) {
    const meta = activeMeta && typeof activeMeta === "object" ? activeMeta : {};
    const fields = {};
    if (meta.active_version_id != null) fields.expected_active_version_id = meta.active_version_id;
    if (meta.active_hash != null) fields.expected_active_hash = meta.active_hash;
    return fields;
  }

  // Load active published version + working draft (+ history).
  async function loadPlaybook() {
    return jsonRequest("/api/playbook/draft", {}, "Playbook could not load");
  }

  // Persist the working clauses to the draft only. Active is untouched.
  async function saveDraft(playbook, { activeMeta } = {}) {
    return jsonRequest(
      "/api/playbook/draft",
      { method: "POST", ...jsonBody({ playbook, ...expectedActiveFields(activeMeta) }) },
      "Draft could not be saved",
    );
  }

  // Validate the working clauses without saving. Returns the raw validation
  // payload; callers normalize via playbook-draft.normalizeValidation.
  async function validateDraft(playbook) {
    return jsonRequest(
      "/api/playbook/validate-draft",
      { method: "POST", ...jsonBody({ playbook }) },
      "Draft could not be validated",
    );
  }

  // Promote the saved draft to active. Sends the playbook so the backend can
  // verify the client and server agree on what is being published.
  async function publishPlaybook(playbook, { activeMeta, actor = "admin" } = {}) {
    return jsonRequest(
      "/api/playbook/publish",
      { method: "POST", ...jsonBody({ playbook, actor, ...expectedActiveFields(activeMeta) }) },
      "Playbook could not be published",
    );
  }

  // Discard the saved draft, returning to the active published version.
  async function discardDraft({ draftId } = {}) {
    const body = draftId ? { draft_id: draftId } : {};
    return jsonRequest(
      "/api/playbook/discard-draft",
      { method: "POST", ...jsonBody(body) },
      "Draft could not be discarded",
    );
  }

  // Restore a historical version into the draft (existing endpoint).
  async function restoreVersion(historyId, actor = "admin") {
    return jsonRequest(
      "/api/playbook/restore",
      { method: "POST", ...jsonBody({ history_id: historyId, actor }) },
      "Playbook version could not be restored",
    );
  }

  return {
    discardDraft,
    loadPlaybook,
    publishPlaybook,
    restoreVersion,
    saveDraft,
    validateDraft,
  };
}
