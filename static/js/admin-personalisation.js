const AdminPersonalisationView = (() => {
  // Self-serve default: every authenticated user (admin or not) reads/writes
  // their OWN signature through /api/me/personalisation-settings. The admin
  // global-default panel passes endpoint="/api/admin/personalisation-settings".
  const SELF_ENDPOINT = "/api/me/personalisation-settings";
  const EMPTY_SETTINGS = { sign_off: "", signature: "", signature_block: "" };

  function createController({
    endpoint,
    card,
    form,
    signOffInput,
    signatureInput,
    signatureBlockInput,
    saveButton,
    resetButton,
    overall,
    message,
    persistenceFact,
    reviewErrorFromPayload,
    onSettingsLoaded,
    // When true (the admin global-default panel), a 403/404 means "not an admin"
    // and the surface hides itself rather than nagging — it is an optional,
    // admin-only section, not the self-serve form every user gets.
    adminOnly = false,
    onUnavailable,
  }) {
    const ENDPOINT = endpoint || SELF_ENDPOINT;
    let loadedSettings = { ...EMPTY_SETTINGS };
    let endpointAvailable = false;
    let loading = false;

    form?.addEventListener("submit", save);
    resetButton?.addEventListener("click", () => {
      renderFields(loadedSettings);
      updateDirtyState();
      setMessage(endpointAvailable ? "Changes reset to the last saved values." : backendContractMessage());
    });
    [signOffInput, signatureInput, signatureBlockInput].forEach((input) => {
      input?.addEventListener("input", updateDirtyState);
    });

    async function load() {
      if (!card || loading) return;
      loading = true;
      endpointAvailable = false;
      setOverall("Checking", "pending");
      setControlsDisabled(true);
      setMessage("Checking personalisation settings...");
      setPersistence("Checking backend support");
      try {
        const response = await fetch(ENDPOINT);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw responseError(response, payload, "Personalisation settings could not load");
        endpointAvailable = true;
        loadedSettings = normaliseSettings(
          payload.personalisation || payload.personalization || payload.settings || {},
        );
        onSettingsLoaded?.(loadedSettings, payload);
        renderFields(loadedSettings, defaultsFromPayload(payload));
        setControlsDisabled(false);
        setOverall("Ready", "ready");
        setMessage(loadedMessage(payload));
        setPersistence(`Saved through ${ENDPOINT}`);
        updateDirtyState();
      } catch (error) {
        endpointAvailable = false;
        loadedSettings = { ...EMPTY_SETTINGS };
        // An admin-only panel that the caller cannot reach (403/404) is not a
        // dead-end: hand off to onUnavailable so the page can hide it entirely.
        if (adminOnly && error?.adminForbidden) {
          onUnavailable?.(error);
          return;
        }
        renderFields(loadedSettings);
        setControlsDisabled(true);
        setOverall("Unavailable", "blocked");
        setMessage(error?.missingEndpoint ? backendContractMessage() : (error.message || "Personalisation settings could not load"));
        setPersistence("Backend endpoint required");
      } finally {
        loading = false;
      }
    }

    async function save(event) {
      event.preventDefault();
      if (!endpointAvailable) {
        setMessage(backendContractMessage());
        return;
      }
      const nextSettings = currentSettings();
      setSaveDisabled(true);
      setOverall("Saving", "pending");
      setMessage("Saving personalisation settings...");
      try {
        const response = await fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(nextSettings),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw responseError(response, payload, "Personalisation settings could not save");
        loadedSettings = normaliseSettings(
          payload.personalisation || payload.personalization || payload.settings || nextSettings,
        );
        onSettingsLoaded?.(loadedSettings, payload);
        renderFields(loadedSettings, defaultsFromPayload(payload));
        setOverall("Saved", "ready");
        setMessage(savedMessage(payload));
      } catch (error) {
        setOverall("Save failed", "blocked");
        setMessage(error.message || "Personalisation settings could not save");
      } finally {
        updateDirtyState();
      }
    }

    function loadedMessage(payload) {
      if (ENDPOINT === SELF_ENDPOINT) {
        return payload && payload.is_custom
          ? "Your signature is loaded. This is what goes out on your emails."
          : "Showing the current default signature. Save to make it your own.";
      }
      return "Personalisation settings loaded.";
    }

    function savedMessage(payload) {
      if (ENDPOINT === SELF_ENDPOINT) {
        return "Your signature is saved. New emails will use it.";
      }
      return "Personalisation settings saved.";
    }

    function defaultsFromPayload(payload = {}) {
      // The /api/me/ payload exposes the inherited org default as global_default;
      // the admin endpoint exposes built-in fallbacks as defaults.
      return payload.global_default || payload.defaults || {};
    }

    function renderFields(settings, defaults = {}) {
      const values = normaliseSettings(settings);
      const fallback = normaliseSettings(defaults);
      if (signOffInput) {
        signOffInput.value = values.sign_off;
        signOffInput.placeholder = fallback.sign_off || "Best,";
      }
      if (signatureInput) {
        signatureInput.value = values.signature;
        signatureInput.placeholder = fallback.signature || "Aspora Legal";
      }
      if (signatureBlockInput) {
        signatureBlockInput.value = values.signature_block;
        signatureBlockInput.placeholder = fallback.signature_block || "Best,\nAspora Legal";
      }
    }

    function currentSettings() {
      return normaliseSettings({
        sign_off: signOffInput?.value,
        signature: signatureInput?.value,
        signature_block: signatureBlockInput?.value,
      });
    }

    function updateDirtyState() {
      const dirty = JSON.stringify(currentSettings()) !== JSON.stringify(loadedSettings);
      setSaveDisabled(!endpointAvailable || !dirty);
      if (resetButton) resetButton.disabled = !endpointAvailable || !dirty;
    }

    function setControlsDisabled(disabled) {
      [signOffInput, signatureInput, signatureBlockInput].forEach((input) => {
        if (input) input.disabled = Boolean(disabled);
      });
      setSaveDisabled(true);
      if (resetButton) resetButton.disabled = true;
    }

    function setSaveDisabled(disabled) {
      if (saveButton) saveButton.disabled = Boolean(disabled);
    }

    function setOverall(label, tone) {
      if (!overall) return;
      overall.textContent = label;
      overall.classList.toggle("ready", tone === "ready");
      overall.classList.toggle("blocked", tone === "blocked");
      overall.classList.toggle("pending", tone === "pending");
    }

    function setMessage(text) {
      if (message) message.textContent = text;
    }

    function setPersistence(text) {
      if (persistenceFact) persistenceFact.textContent = text;
    }

    function responseError(response, payload, fallback) {
      const error = new Error(reviewErrorFromPayload?.(payload, fallback)?.message || payload?.error || fallback);
      error.missingEndpoint = response.status === 404;
      // 403 (or a 404 on an admin-only endpoint) means the caller is not an
      // admin — the admin global-default panel uses this to hide itself.
      error.adminForbidden = response.status === 403 || response.status === 404;
      return error;
    }

    return { load };
  }

  function normaliseSettings(payload = {}) {
    return {
      sign_off: String(payload.sign_off ?? payload.signOff ?? "").trim(),
      signature: String(payload.signature ?? "").trim(),
      signature_block: String(payload.signature_block ?? payload.signatureBlock ?? "").trim(),
    };
  }

  function backendContractMessage() {
    return "Backend support needed: GET/POST /api/me/personalisation-settings with sign_off, signature, and signature_block.";
  }

  return { createController, SELF_ENDPOINT };
})();

function createAdminPersonalisationController(options) {
  return AdminPersonalisationView.createController(options);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminPersonalisationView, createAdminPersonalisationController };
}
