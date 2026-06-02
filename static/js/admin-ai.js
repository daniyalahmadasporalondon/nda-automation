const AdminAiView = (() => {
  function createController({
    state,
    aiCard,
    aiEnabledToggle,
    aiFacts,
    aiOverall,
    aiRefreshButton,
    reviewErrorFromPayload,
  }) {
    aiRefreshButton?.addEventListener("click", load);
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
      setFact("api-key", keyConfigured ? "Configured in backend environment" : "Missing GEMINI_API_KEY");
      setFact("confidence-threshold", String(status.confidence_threshold ?? "-"));
      setFact("target-clauses", targetClausesLabel(status.target_clause_ids));
      setFact("source", sourceLabel(status));
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

    function sourceLabel(status) {
      if (typeof status.stored_enabled === "boolean") return "Admin toggle";
      if (status.environment_enabled === true) return "NDA_AI_REVIEW_ENABLED environment variable";
      return "Default off";
    }

    return { load };
  }

  return { createController };
})();

function createAdminAiController(options) {
  return AdminAiView.createController(options);
}
