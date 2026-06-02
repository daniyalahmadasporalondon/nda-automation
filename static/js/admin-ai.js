const AdminAiView = (() => {
  function createController({
    state,
    aiCard,
    aiKeyForm,
    aiApiKeyInput,
    aiClearKeyButton,
    aiEnabledToggle,
    aiFacts,
    aiOverall,
    aiRefreshButton,
    reviewErrorFromPayload,
  }) {
    aiRefreshButton?.addEventListener("click", load);
    aiKeyForm?.addEventListener("submit", saveAiKey);
    aiClearKeyButton?.addEventListener("click", clearAiKey);
    aiEnabledToggle?.addEventListener("click", updateAiEnabled);

    async function load() {
      if (!aiCard) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/ai/settings");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "AI settings could not load");
        renderAi(payload.ai_review || {});
      } catch (error) {
        renderError(error.message || "AI settings could not load");
      }
    }

    async function saveAiKey(event) {
      event.preventDefault();
      const apiKey = aiApiKeyInput?.value.trim() || "";
      if (!apiKey) {
        setFact("key-message", "Paste a Gemini API key first.");
        aiApiKeyInput?.focus();
        return;
      }
      setKeyControlsDisabled(true);
      setOverall("Saving", "pending");
      setFact("key-message", "Saving key and turning AI on...");
      try {
        const response = await fetch("/api/ai/api-key", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey, enabled: true }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "AI key could not save");
        if (aiApiKeyInput) aiApiKeyInput.value = "";
        renderAi(payload.ai_review || {});
        setFact("key-message", "Gemini key saved locally. AI is on.");
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        setFact("key-message", error.message || "AI key could not save");
      } finally {
        setKeyControlsDisabled(false);
      }
    }

    async function clearAiKey() {
      setKeyControlsDisabled(true);
      setOverall("Saving", "pending");
      setFact("key-message", "Clearing saved local key...");
      try {
        const response = await fetch("/api/ai/api-key", { method: "DELETE" });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Saved AI key could not clear");
        renderAi(payload.ai_review || {});
        setFact("key-message", payload.ai_review?.api_key_source === "environment"
          ? "Local key cleared. The backend environment key is still configured."
          : "Saved local key cleared.");
      } catch (error) {
        setOverall(error.message || "Clear failed", "blocked");
        setFact("key-message", error.message || "Saved AI key could not clear");
      } finally {
        setKeyControlsDisabled(false);
      }
    }

    async function updateAiEnabled() {
      const nextEnabled = state.aiReviewStatus?.enabled !== true;
      setToggleDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/ai/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: nextEnabled }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "AI setting could not save");
        renderAi(payload.ai_review || {});
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderToggle(state.aiReviewStatus?.enabled === true);
      } finally {
        setToggleDisabled(false);
      }
    }

    function renderAi(status) {
      state.aiReviewStatus = status;
      const enabled = status.enabled === true;
      const keyConfigured = status.api_key_configured === true;
      setOverall(enabled ? (keyConfigured ? "On" : "Needs key") : "Off", enabled ? (keyConfigured ? "ready" : "blocked") : "pending");
      renderToggle(enabled);
      setFact("enabled-copy", enabled ? (keyConfigured ? "On" : "On - missing GEMINI_API_KEY") : "Off");
      setFact("provider", status.provider || "gemini");
      setFact("model", status.model || "-");
      setFact("api-key", apiKeyLabel(status));
      setFact("confidence-threshold", String(status.confidence_threshold ?? "-"));
      setFact("target-clauses", targetClausesLabel(status.target_clause_ids));
      setFact("source", sourceLabel(status));
      setFact("key-message", keyMessage(status));
    }

    function renderError(message) {
      setOverall("Unavailable", "blocked");
      setFact("enabled-copy", message);
      setFact("provider", "Unknown");
      setFact("model", "Unknown");
      setFact("api-key", "Unknown");
      setFact("confidence-threshold", "Unknown");
      setFact("target-clauses", "Unknown");
      setFact("source", "Unknown");
      setFact("key-message", message);
    }

    function renderToggle(enabled) {
      if (!aiEnabledToggle) return;
      aiEnabledToggle.setAttribute("aria-checked", enabled ? "true" : "false");
      aiEnabledToggle.classList.toggle("on", enabled);
      aiEnabledToggle.classList.toggle("off", !enabled);
    }

    function setToggleDisabled(disabled) {
      if (aiEnabledToggle) aiEnabledToggle.disabled = disabled;
    }

    function setKeyControlsDisabled(disabled) {
      if (aiApiKeyInput) aiApiKeyInput.disabled = disabled;
      if (aiKeyForm) {
        aiKeyForm.querySelectorAll("button").forEach((button) => {
          button.disabled = disabled;
        });
      }
      if (aiClearKeyButton) aiClearKeyButton.disabled = disabled;
    }

    function setOverall(label, tone) {
      if (!aiOverall) return;
      aiOverall.textContent = label;
      aiOverall.classList.toggle("ready", tone === "ready");
      aiOverall.classList.toggle("blocked", tone === "blocked");
      aiOverall.classList.toggle("pending", tone === "pending");
    }

    function setFact(key, value) {
      const node = aiCard?.querySelector(`[data-admin-ai="${key}"]`) || aiFacts?.querySelector(`[data-admin-ai="${key}"]`);
      if (node) node.textContent = value;
    }

    function targetClausesLabel(values) {
      if (!Array.isArray(values) || !values.length) return "None";
      return values.join(", ");
    }

    function apiKeyLabel(status) {
      if (status.api_key_source === "environment") return "Configured in backend environment";
      if (status.api_key_source === "local_settings") return "Configured from saved local key";
      return "Missing Gemini API key";
    }

    function sourceLabel(status) {
      if (typeof status.stored_enabled === "boolean") return "Admin toggle";
      if (status.environment_enabled === true) return "NDA_AI_REVIEW_ENABLED environment variable";
      return "Default off";
    }

    function keyMessage(status) {
      if (status.api_key_source === "environment") return "Using GEMINI_API_KEY from the backend environment.";
      if (status.api_key_source === "local_settings") return "Using a saved local key under ignored app data.";
      return "Paste a Gemini key and click Save key & turn on.";
    }

    return { load };
  }

  return { createController };
})();

function createAdminAiController(options) {
  return AdminAiView.createController(options);
}
