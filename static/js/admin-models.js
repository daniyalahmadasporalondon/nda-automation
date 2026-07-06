// Admin AI Models panel: per-role model picker (admin-models.js).
//
// Mirrors admin-ai.js / admin-access.js -- a GET probe on load (a 403 means
// "not an admin", rendered as a calm read-only state rather than an error), one
// editor ROW per AI role (effective model + source badge + a recommended-model
// dropdown with a "Custom..." free-text escape hatch + a Reset-to-default), and
// a single Save that POSTs ONLY the changed roles to /api/ai/models and
// re-renders from the authoritative response.
//
// The eleven roles arrive in the GET payload's `ai_models` array (already in the
// canonical UI order from the backend's model_resolver.ROLES); we render them in
// that exact order. Each entry:
//   { role, model (effective), source: "persisted"|"env"|"default",
//     env_var, default, recommended: [...] }
//
// CSRF: like every other admin POST in this app, a same-origin fetch carries the
// browser-attached Origin header, which the server's Origin-based CSRF check
// (csrf.py) accepts -- so NO per-form token is sent (none exists). This matches
// admin-ai.js / admin-access.js exactly.
//
// Every interpolated value (role labels are static, but model ids / env vars are
// server-supplied) is HTML-escaped (no innerHTML injection).
const AdminModelsView = (() => {
  function esc(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") {
      return window.escapeHtml(value);
    }
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  // Human label per role. Keeps raw snake_case role ids off the admin screen.
  // An unmapped role (e.g. a future backend role) falls back to the shared
  // generic humanizer so it still reads as English rather than leaking the id.
  const ROLE_LABELS = {
    reviewer: "Reviewer",
    verifier: "Verifier",
    structure: "Structure validation",
    semantic_lint: "Semantic lint",
    generation: "Generation",
    gmail_triage: "Gmail triage",
    gmail_intake: "Gmail intake",
    pdf_ocr: "PDF OCR",
    dashboard_assistant: "Dashboard assistant",
    search_intent: "Search intent",
    matter_summary: "Matter summary",
  };

  // The three roles that USED to silently ride the reviewer's model and are now
  // independently configurable. Grouped + labelled so it's obvious they're
  // decoupled (an admin changing the reviewer no longer drags these along).
  const DECOUPLED_ROLES = new Set(["dashboard_assistant", "search_intent", "matter_summary"]);

  function roleLabel(role) {
    if (Object.prototype.hasOwnProperty.call(ROLE_LABELS, role)) return ROLE_LABELS[role];
    if (typeof window !== "undefined" && typeof window.humanizeId === "function") {
      return window.humanizeId(role);
    }
    return String(role || "");
  }

  function sourceBadge(source) {
    if (source === "persisted") return { label: "Admin override", tone: "persisted" };
    if (source === "env") return { label: "From env", tone: "env" };
    return { label: "Default", tone: "default" };
  }

  // The sentinel <option> value that reveals the free-text custom input. A real
  // model id never equals this (it is not a valid OpenRouter slug).
  const CUSTOM_VALUE = "__custom__";

  function createController({
    card,
    overall,
    refreshButton,
    rowsList,
    saveButton,
    resetAllButton,
    message,
    warningNote,
    reviewErrorFromPayload,
  }) {
    refreshButton?.addEventListener("click", load);
    saveButton?.addEventListener("click", save);

    // Event-delegated row interactions survive the full re-render on every load:
    //   * a per-role <select> change toggles the Custom free-text input;
    //   * a per-role "Reset to default" button clears that one override.
    rowsList?.addEventListener("change", (event) => {
      const select = event.target?.closest?.("[data-model-select]");
      if (select) onSelectChange(select);
    });
    rowsList?.addEventListener("click", (event) => {
      const button = event.target?.closest?.("[data-model-reset]");
      if (!button) return;
      event.preventDefault();
      resetRole(button.getAttribute("data-model-reset"));
    });

    // The last server-rendered overview, keyed by role. The Save diff compares
    // each row's CURRENT editor value against this effective model so only
    // genuinely-changed roles are sent.
    let lastByRole = {};

    async function load() {
      if (!card) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/ai/settings");
        // The AI config read is admin-only. A non-admin still loads the app shell
        // (and can USE the AI review), so a 403 here is expected, not a failure --
        // render a calm "admin only" state, never the raw 403 error text.
        if (response.status === 403) {
          renderAdminOnly();
          return;
        }
        const payload = await window.AuthExpired.parseOkJson(
          response,
          "AI model settings could not load",
          reviewErrorFromPayload,
        );
        renderFromPayload(payload);
      } catch (error) {
        renderError(error.message || "AI model settings could not load");
      }
    }

    function renderAdminOnly() {
      setOverall("Admin only", "pending");
      setControlsDisabled(true);
      if (rowsList) rowsList.innerHTML = "";
      setMessage("AI model selection is managed by an administrator.");
      renderWarnings([]);
    }

    function renderError(text) {
      setOverall("Unavailable", "blocked");
      setMessage(text);
    }

    function renderFromPayload(payload = {}) {
      const models = Array.isArray(payload.ai_models) ? payload.ai_models : [];
      const warnings = Array.isArray(payload.operational_warnings) ? payload.operational_warnings : [];
      render(models, warnings);
    }

    function render(models, warnings = []) {
      lastByRole = {};
      models.forEach((entry) => {
        if (entry && typeof entry.role === "string") lastByRole[entry.role] = entry;
      });
      setOverall(models.length ? `${models.length} roles` : "No roles", models.length ? "ready" : "blocked");
      setControlsDisabled(false);
      renderRows(models);
      renderWarnings(warnings);
      setMessage("Pick a model per role, then Save. Changes take effect on the next request (no restart).");
    }

    function renderRows(models) {
      if (!rowsList) return;
      if (!models.length) {
        rowsList.innerHTML = `<li class="admin-models-empty">No AI roles were returned.</li>`;
        return;
      }
      rowsList.innerHTML = models.map((entry) => rowHtml(entry)).join("");
    }

    function rowHtml(entry = {}) {
      const role = String(entry.role || "");
      const effective = String(entry.model || "");
      const badge = sourceBadge(entry.source);
      const recommended = Array.isArray(entry.recommended) ? entry.recommended.map(String) : [];
      const envVar = String(entry.env_var || "");
      const defaultModel = String(entry.default || "");
      // Pre-select the effective model if it's in the recommended list; otherwise
      // the row opens on "Custom..." with the effective model in the free-text
      // input (so a hand-set / env / default id that isn't recommended is never
      // silently lost on the next Save).
      const inList = recommended.includes(effective);
      const decoupled = DECOUPLED_ROLES.has(role);
      // `enabled` (backend, informational): false means this role's feature is
      // gated OFF by default, so a picked model never runs. We keep the row fully
      // functional (an admin can pre-set a model for when it's turned on) but make
      // it obvious the pick is currently inert.
      const featureOff = entry.enabled === false;
      const labelId = `adminModelLabel-${esc(role)}`;
      const inputId = `adminModelCustom-${esc(role)}`;

      const options = recommended
        .map((model) => `<option value="${esc(model)}"${model === effective ? " selected" : ""}>${esc(model)}</option>`)
        .join("");
      const customSelected = inList ? "" : " selected";
      const customHidden = inList ? " hidden" : "";
      const customValue = inList ? "" : effective;

      const decoupledTag = decoupled
        ? ` <span class="admin-models-decoupled" title="Independently configurable -- no longer rides the Reviewer model.">Independent</span>`
        : "";

      const featureOffTag = featureOff
        ? ` <span class="admin-models-featureoff" title="This feature is turned off, so the selected model isn't used.">Feature off</span>`
        : "";
      const rowClass = featureOff ? "admin-models-row admin-models-row--off" : "admin-models-row";

      return `
        <li class="${rowClass}" data-model-row="${esc(role)}"${featureOff ? ' data-feature-off="1"' : ""}>
          <div class="admin-models-identity">
            <span class="admin-models-name" id="${labelId}">${esc(roleLabel(role))}${decoupledTag}${featureOffTag}</span>
            <span class="admin-models-effective" title="${esc(effective)}">${esc(effective) || "&mdash;"}</span>
            <span class="admin-models-env" title="Environment variable: ${esc(envVar)}">${esc(envVar)}</span>
          </div>
          <span class="admin-models-badge admin-models-badge--${esc(badge.tone)}">${esc(badge.label)}</span>
          <div class="admin-models-editor">
            <select class="admin-models-select" data-model-select="${esc(role)}" aria-labelledby="${labelId}">
              ${options}
              <option value="${CUSTOM_VALUE}"${customSelected}>Custom&hellip;</option>
            </select>
            <input
              class="admin-models-custom"
              id="${inputId}"
              type="text"
              spellcheck="false"
              autocomplete="off"
              placeholder="provider/model-id"
              data-model-custom="${esc(role)}"
              value="${esc(customValue)}"
              aria-label="Custom model id for ${esc(roleLabel(role))}"${customHidden}>
          </div>
          <button
            class="link-button admin-models-reset"
            type="button"
            data-model-reset="${esc(role)}"
            title="Clear the admin override and revert to ${esc(defaultModel || envVar || "the default")}.">Reset to default</button>
        </li>`;
    }

    function renderWarnings(warnings) {
      if (!warningNote) return;
      const unverified = (warnings || []).filter((w) => w && w.code === "ai_model_unverified");
      if (!unverified.length) {
        warningNote.hidden = true;
        warningNote.textContent = "";
        return;
      }
      // The save SUCCEEDED but the OpenRouter catalog was unreachable, so the
      // model id could not be verified. Surface it as a non-blocking notice.
      const detail = unverified.map((w) => String(w.message || "")).filter(Boolean).join(" ");
      warningNote.hidden = false;
      warningNote.textContent = detail
        ? `Saved, but the model catalog was unreachable so it could not be verified: ${detail}`
        : "Saved, but the model catalog was unreachable so the model could not be verified.";
    }

    // --- Row editor helpers (operate on the live DOM rows) --------------------

    function rowNode(role) {
      return rowsList?.querySelector?.(`[data-model-row="${cssEscape(role)}"]`) || null;
    }

    function selectNode(role) {
      return rowNode(role)?.querySelector?.("[data-model-select]") || null;
    }

    function customNode(role) {
      return rowNode(role)?.querySelector?.("[data-model-custom]") || null;
    }

    // Minimal CSS attribute-selector escaping (role ids are snake_case so this is
    // belt-and-braces; never trust an unescaped interpolation into querySelector).
    function cssEscape(value) {
      if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(value);
      return String(value).replace(/["\\]/g, "\\$&");
    }

    function onSelectChange(select) {
      const role = select.getAttribute("data-model-select");
      const custom = customNode(role);
      if (!custom) return;
      if (select.value === CUSTOM_VALUE) {
        custom.hidden = false;
        custom.focus?.();
      } else {
        custom.hidden = true;
      }
    }

    // The editor's chosen model id for a role: the free-text input when "Custom"
    // is selected, otherwise the dropdown value.
    function editorValue(role) {
      const select = selectNode(role);
      if (!select) return null;
      if (select.value === CUSTOM_VALUE) {
        return (customNode(role)?.value || "").trim();
      }
      return (select.value || "").trim();
    }

    async function resetRole(role) {
      if (!role) return;
      // Reset = clear the override for this one role (send ""), reverting to
      // env/default. Other roles' in-progress edits are NOT touched.
      await postModels({ [role]: "" }, `Reverting ${roleLabel(role)} to default...`);
    }

    async function save() {
      // Diff the editor against the last-rendered effective model so ONLY changed
      // roles are sent (the contract: send only the role(s) being changed).
      const updates = {};
      let invalidRole = null;
      Object.keys(lastByRole).forEach((role) => {
        const next = editorValue(role);
        if (next === null) return; // row not rendered
        const current = String(lastByRole[role]?.model || "");
        if (next === current) return; // unchanged
        // A "Custom" selection with an empty box is ambiguous -- block the save
        // and point the admin at it rather than silently clearing the override.
        if (next === "" && selectNode(role)?.value === CUSTOM_VALUE) {
          invalidRole = role;
          return;
        }
        updates[role] = next;
      });

      if (invalidRole) {
        setMessage(`Enter a model id for ${roleLabel(invalidRole)} (or pick Reset to default to clear it).`);
        customNode(invalidRole)?.focus?.();
        return;
      }
      if (!Object.keys(updates).length) {
        setMessage("No model changes to save.");
        return;
      }
      await postModels(updates, "Saving model changes...");
    }

    async function postModels(updates, pendingMessage) {
      setControlsDisabled(true);
      setMessage(pendingMessage);
      try {
        const response = await fetch("/api/ai/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ models: updates }),
        });
        // A rejected save returns 400 {error} (bad model id / unknown role /
        // empty) and persists NOTHING; parseOkJson throws the server's {error}
        // into the catch below, where we surface it inline and DO NOT re-render
        // (so the admin's other in-progress edits survive).
        const payload = await window.AuthExpired.parseOkJson(
          response,
          "Model changes could not save",
          reviewErrorFromPayload,
        );
        renderFromPayload(payload);
        const warned = (Array.isArray(payload.operational_warnings) ? payload.operational_warnings : [])
          .some((w) => w && w.code === "ai_model_unverified");
        setMessage(warned ? "Saved (see the catalog notice above)." : "Model changes saved.");
      } catch (error) {
        // 400 (reject) or 403 (not admin): surface the reason inline. Keep the
        // current rows + their edits intact -- nothing was saved server-side.
        setMessage(error.message || "Model changes could not save");
      } finally {
        setControlsDisabled(false);
      }
    }

    function setControlsDisabled(disabled) {
      if (saveButton) saveButton.disabled = disabled;
      if (resetAllButton) resetAllButton.disabled = disabled;
      rowsList?.querySelectorAll?.("select, input, [data-model-reset]").forEach((node) => {
        node.disabled = disabled;
      });
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

    return { load };
  }

  return { createController };
})();

function createAdminModelsController(options) {
  return AdminModelsView.createController(options);
}

// CommonJS export for the Node test harness (a no-op in the browser, where this
// file is loaded as a classic script and `module` is undefined).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminModelsView, createAdminModelsController };
}
