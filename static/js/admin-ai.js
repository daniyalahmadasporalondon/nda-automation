const AdminAiView = (() => {
  function createController({
    state,
    aiCard,
    aiKeyForm,
    aiApiKeyInput,
    aiClearKeyButton,
    aiEnabledToggle,
    runtimeForm,
    activeReviewEngineSelect,
    runtimeSaveButton,
    aiFacts,
    aiOverall,
    aiRefreshButton,
    reviewErrorFromPayload,
  }) {
    aiRefreshButton?.addEventListener("click", load);
    aiKeyForm?.addEventListener("submit", saveAiKey);
    aiClearKeyButton?.addEventListener("click", clearAiKey);
    aiEnabledToggle?.addEventListener("click", updateAiEnabled);
    runtimeForm?.addEventListener("submit", saveRuntimeSettings);

    async function load() {
      if (!aiCard) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/ai/settings");
        const payload = await window.AuthExpired.parseOkJson(response, "AI settings could not load", reviewErrorFromPayload);
        renderAiFromPayload(payload);
      } catch (error) {
        renderError(error.message || "AI settings could not load");
      }
    }

    async function saveAiKey(event) {
      event.preventDefault();
      const apiKey = aiApiKeyInput?.value.trim() || "";
      if (!apiKey) {
        setFact("key-message", "Paste an OpenRouter API key first.");
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
        const payload = await window.AuthExpired.parseOkJson(response, "AI key could not save", reviewErrorFromPayload);
        if (aiApiKeyInput) aiApiKeyInput.value = "";
        renderAiFromPayload(payload);
        setFact("key-message", "AI key saved locally. AI is on.");
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
        const payload = await window.AuthExpired.parseOkJson(response, "Saved AI key could not clear", reviewErrorFromPayload);
        renderAiFromPayload(payload);
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
        const payload = await window.AuthExpired.parseOkJson(response, "AI setting could not save", reviewErrorFromPayload);
        renderAiFromPayload(payload);
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderToggle(state.aiReviewStatus?.enabled === true);
      } finally {
        setToggleDisabled(false);
      }
    }

    async function saveRuntimeSettings(event) {
      event.preventDefault();
      const activeReviewEngine = activeReviewEngineSelect?.value || "ai_first";
      const runtimeStatus = state.activeReviewEngineStatus || {};
      const requestPayload = {};
      if (!runtimeStatus.environment_active_engine) {
        requestPayload.active_review_engine = activeReviewEngine;
      }
      if (!Object.keys(requestPayload).length) {
        setFact("runtime-message", "Runtime settings are pinned by the backend environment.");
        return;
      }
      setRuntimeControlsDisabled(true);
      setFact("runtime-message", "Saving runtime settings...");
      try {
        const response = await fetch("/api/ai/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestPayload),
        });
        const payload = await window.AuthExpired.parseOkJson(response, "Runtime settings could not save", reviewErrorFromPayload);
        renderAiFromPayload(payload);
        setFact("runtime-message", "Runtime settings saved for new reviews.");
      } catch (error) {
        setFact("runtime-message", error.message || "Runtime settings could not save");
      } finally {
        setRuntimeControlsDisabled(false);
      }
    }

    function renderAiFromPayload(payload = {}) {
      renderAi(
        payload.ai_review || {},
        payload.ai_verifier || {},
        payload.active_review_engine || {},
        Array.isArray(payload.operational_warnings) ? payload.operational_warnings : [],
        Array.isArray(payload.settings_audit) ? payload.settings_audit : [],
      );
    }

    function renderAi(status, verifierStatus = {}, runtimeStatus = {}, warnings = [], settingsAudit = []) {
      state.aiReviewStatus = status;
      state.aiVerifierStatus = verifierStatus;
      state.activeReviewEngineStatus = runtimeStatus;
      state.operationalWarnings = warnings;
      state.settingsAudit = settingsAudit;
      const enabled = status.enabled === true;
      const keyConfigured = status.api_key_configured === true;
      setOverall(enabled ? (keyConfigured ? "On" : "Needs key") : "Off", enabled ? (keyConfigured ? "ready" : "blocked") : "pending");
      renderToggle(enabled);
      renderRuntime(runtimeStatus, status);
      renderOperationalStatus(warnings, settingsAudit);
      setFact("enabled-copy", enabled ? (keyConfigured ? "On" : "On - missing API key") : "Off");
      setFact("provider", status.provider || "openrouter");
      setFact("model", status.model || "-");
      setFact("api-key", apiKeyLabel(status));
      setFact("confidence-threshold", String(status.confidence_threshold ?? "-"));
      setFact("target-clauses", targetClausesLabel(status.target_clause_ids));
      setFact("verifier-kind", verifierKindLabel(verifierStatus));
      setFact("verifier-model", verifierModelLabel(verifierStatus));
      setFact("verifier-key", verifierKeyLabel(verifierStatus));
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
      setFact("verifier-kind", "Unknown");
      setFact("verifier-model", "Unknown");
      setFact("verifier-key", "Unknown");
      setFact("source", "Unknown");
      setFact("key-message", message);
      setFact("active-engine", "Unknown");
      setFact("runtime-source", "Unknown");
      setFact("operational-warnings", "Unknown");
      setFact("last-settings-change", "Unknown");
      setFact("runtime-message", message);
    }

    function renderRuntime(status, aiStatus = {}) {
      const activeEngine = runtimeValue(status.active_engine, "ai_first");
      if (activeReviewEngineSelect) activeReviewEngineSelect.value = activeEngine;
      setFact("active-engine", engineLabel(activeEngine));
      setFact("runtime-source", runtimeSourceLabel(status));
      const missingAiKey = activeEngine === "ai_first" && aiStatus.api_key_configured !== true;
      setFact("runtime-message", missingAiKey
        ? "AI-first is active. Add an AI key before running reviews."
        : "Runtime changes apply to new reviews.");
      setRuntimeControlsDisabled(false);
    }

    function renderOperationalStatus(warnings, settingsAudit) {
      if (warnings.length) {
        setFact("operational-warnings", warnings.map((warning) => warning.message || warning.code).filter(Boolean).join(" "));
      } else {
        setFact("operational-warnings", "None");
      }
      const latest = settingsAudit.find((event) => event?.recorded_at);
      if (!latest) {
        setFact("last-settings-change", "None");
        return;
      }
      const changedSettings = Array.isArray(latest.changes)
        ? latest.changes
            .map((change) => change.setting)
            .filter((setting) => setting && !String(setting).includes("fallback"))
            .join(", ")
        : "";
      setFact("last-settings-change", changedSettings ? `${latest.action}: ${changedSettings}` : latest.action || "settings_update");
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

    function setRuntimeControlsDisabled(disabled) {
      const runtimeStatus = state.activeReviewEngineStatus || {};
      const enginePinned = Boolean(runtimeStatus.environment_active_engine);
      if (activeReviewEngineSelect) activeReviewEngineSelect.disabled = disabled || enginePinned;
      if (runtimeSaveButton) runtimeSaveButton.disabled = disabled || enginePinned;
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

    function verifierKindLabel(status = {}) {
      if (status.active_kind === "ai") return "AI via OpenRouter";
      // The verifier is AI-only: when it is not the AI pass it is a no-op that
      // changes no verdicts (it never falls back to a deterministic/regex engine).
      if (status.enabled === true) return "Inactive (no OpenRouter key)";
      return "Inactive (AI verifier off)";
    }

    function verifierModelLabel(status = {}) {
      return status.model || status.default_model || "deepseek/deepseek-v4-pro";
    }

    function verifierKeyLabel(status = {}) {
      if (status.api_key_source === "environment") return "Configured in backend environment";
      if (status.api_key_source === "local_settings") return "Configured from saved local OpenRouter key";
      return status.enabled === true ? "Missing OpenRouter key" : "Not required while AI verifier is off";
    }

    function apiKeyLabel(status) {
      if (status.api_key_source === "environment") return "Configured in backend environment";
      if (status.api_key_source === "local_settings") return `Configured from saved local ${providerName(status.provider)} key`;
      return "Missing AI API key";
    }

    function sourceLabel(status) {
      if (typeof status.stored_enabled === "boolean") return "Admin toggle";
      if (status.environment_enabled === true) return "NDA_AI_REVIEW_ENABLED environment variable";
      return "Default off";
    }

    function runtimeSourceLabel(status) {
      const engineSource = String(status.engine_source || "");
      if (engineSource === "environment") return "Backend environment";
      if (engineSource === "runtime_settings") return "Admin runtime settings";
      return "Default runtime";
    }

    function runtimeValue(value, fallback) {
      const normalized = String(value || "").trim();
      return normalized || fallback;
    }

    function engineLabel(engine) {
      return engine === "ai_first" ? "AI-first" : String(engine || "Unknown");
    }

    function keyMessage(status) {
      if (status.api_key_source === "environment") {
        return "Using OPENROUTER_API_KEY from the backend environment.";
      }
      if (status.api_key_source === "local_settings") return `Using a saved local ${providerName(status.provider)} key under ignored app data.`;
      return "Paste an OpenRouter key and click Save key & turn on.";
    }

    function providerName(provider) {
      return "OpenRouter";
    }

    return { load };
  }

  return { createController };
})();

function createAdminAiController(options) {
  return AdminAiView.createController(options);
}
