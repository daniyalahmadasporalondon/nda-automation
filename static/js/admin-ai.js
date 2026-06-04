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
    aiFirstFallbackSelect,
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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "AI settings could not load");
        renderAiFromPayload(payload);
      } catch (error) {
        renderError(error.message || "AI settings could not load");
      }
    }

    async function saveAiKey(event) {
      event.preventDefault();
      const apiKey = aiApiKeyInput?.value.trim() || "";
      if (!apiKey) {
        setFact("key-message", "Paste a Gemini, OpenRouter, or Alibaba API key first.");
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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Saved AI key could not clear");
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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "AI setting could not save");
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
      const fallbackMode = aiFirstFallbackSelect?.value || "fail_closed";
      const runtimeStatus = state.activeReviewEngineStatus || {};
      const requestPayload = {};
      if (!runtimeStatus.environment_active_engine) {
        requestPayload.active_review_engine = activeReviewEngine;
      }
      if (!runtimeStatus.environment_ai_first_fallback_mode) {
        requestPayload.ai_first_fallback_mode = fallbackMode;
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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Runtime settings could not save");
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
        payload.active_review_engine || {},
        Array.isArray(payload.operational_warnings) ? payload.operational_warnings : [],
        Array.isArray(payload.settings_audit) ? payload.settings_audit : [],
      );
    }

    function renderAi(status, runtimeStatus = {}, warnings = [], settingsAudit = []) {
      state.aiReviewStatus = status;
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
      setFact("active-engine", "Unknown");
      setFact("fallback-mode", "Unknown");
      setFact("runtime-source", "Unknown");
      setFact("operational-warnings", "Unknown");
      setFact("last-settings-change", "Unknown");
      setFact("runtime-message", message);
    }

    function renderRuntime(status, aiStatus = {}) {
      const activeEngine = runtimeValue(status.active_engine, "ai_first");
      const fallbackMode = runtimeValue(status.ai_first_fallback_mode, "fail_closed");
      if (activeReviewEngineSelect) activeReviewEngineSelect.value = activeEngine;
      if (aiFirstFallbackSelect) aiFirstFallbackSelect.value = fallbackMode;
      setFact("active-engine", engineLabel(activeEngine));
      setFact("fallback-mode", fallbackLabel(fallbackMode));
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
        ? latest.changes.map((change) => change.setting).filter(Boolean).join(", ")
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
      const fallbackPinned = Boolean(runtimeStatus.environment_ai_first_fallback_mode);
      if (activeReviewEngineSelect) activeReviewEngineSelect.disabled = disabled || enginePinned;
      if (aiFirstFallbackSelect) aiFirstFallbackSelect.disabled = disabled || fallbackPinned;
      if (runtimeSaveButton) runtimeSaveButton.disabled = disabled || (enginePinned && fallbackPinned);
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
      const fallbackSource = String(status.fallback_source || "");
      if (engineSource === "environment" || fallbackSource === "environment") return "Backend environment";
      if (engineSource === "runtime_settings" || fallbackSource === "runtime_settings") return "Admin runtime settings";
      return "Default runtime";
    }

    function runtimeValue(value, fallback) {
      const normalized = String(value || "").trim();
      return normalized || fallback;
    }

    function engineLabel(engine) {
      return engine === "ai_first" ? "AI-first" : "Deterministic";
    }

    function fallbackLabel(mode) {
      return mode === "fail_closed" ? "Fail closed" : "Deterministic fallback";
    }

    function keyMessage(status) {
      if (status.api_key_source === "environment") {
        if (status.provider === "openrouter") return "Using OPENROUTER_API_KEY from the backend environment.";
        if (status.provider === "alibaba") return "Using ALIBABA_API_KEY or DASHSCOPE_API_KEY from the backend environment.";
        return "Using GEMINI_API_KEY from the backend environment.";
      }
      if (status.api_key_source === "local_settings") return `Using a saved local ${providerName(status.provider)} key under ignored app data.`;
      return "Paste a Gemini, OpenRouter, or Alibaba key and click Save key & turn on.";
    }

    function providerName(provider) {
      if (provider === "openrouter") return "OpenRouter";
      if (provider === "alibaba") return "Alibaba/Qwen";
      return "Gemini";
    }

    return { load };
  }

  return { createController };
})();

function createAdminAiController(options) {
  return AdminAiView.createController(options);
}
