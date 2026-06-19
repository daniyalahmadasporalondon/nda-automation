// Admin Access panel: add/remove in-app admins by email. Mirrors admin-ai.js /
// admin-drive.js -- a GET probe on load (a 403 means "not an admin", rendered as
// a calm read-only state rather than an error), an add-by-email form, and a
// per-row Remove button for persisted admins. Env-root (bootstrap) admins are
// listed with an immutable badge and no Remove button. Every interpolated value
// is escaped (no innerHTML injection of emails/ids).
const AdminAccessView = (() => {
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

  // Parse an ISO-8601 added_at string into a concise local date+time, e.g.
  // "19 Jun 2026, 10:21". Older entries may carry a missing/blank/unparseable
  // value; in that case return "" so the caller can omit the date entirely
  // (no "Invalid Date" ever reaches the DOM). The generated string is composed
  // from numeric/whitespace pieces only and is safe to interpolate as-is.
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function formatAddedAt(value) {
    const raw = typeof value === "string" ? value.trim() : value;
    if (!raw) return "";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return "";
    const day = date.getDate();
    const month = MONTHS[date.getMonth()];
    const year = date.getFullYear();
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    return `${day} ${month} ${year}, ${hours}:${minutes}`;
  }

  function createController({
    card,
    overall,
    refreshButton,
    addForm,
    emailInput,
    addButton,
    message,
    envRootsList,
    persistedList,
    reviewErrorFromPayload,
  }) {
    refreshButton?.addEventListener("click", load);
    addForm?.addEventListener("submit", addAdmin);
    // Event-delegated Remove: the rows are re-rendered from the server's full
    // list on every mutation, so a delegated listener survives re-render.
    persistedList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-admin-remove]");
      if (!button) return;
      removeAdmin(button.getAttribute("data-admin-remove"));
    });

    async function load() {
      if (!card) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/admin/admins");
        // The admin list is admin-only. A non-admin still loads the app shell, so
        // a 403 here is expected -- render a calm "admin only" state, not an error.
        if (response.status === 403) {
          renderAdminOnly();
          return;
        }
        const payload = await window.AuthExpired.parseOkJson(
          response,
          "Admin list could not load",
          reviewErrorFromPayload,
        );
        render(payload);
      } catch (error) {
        renderError(error.message || "Admin list could not load");
      }
    }

    function renderAdminOnly() {
      setOverall("Admin only", "pending");
      setControlsDisabled(true);
      if (envRootsList) envRootsList.innerHTML = "";
      if (persistedList) persistedList.innerHTML = "";
      setMessage("Admin access is managed by an administrator.");
    }

    function renderError(text) {
      setOverall("Unavailable", "blocked");
      setMessage(text);
    }

    async function addAdmin(event) {
      event.preventDefault();
      const email = (emailInput?.value || "").trim();
      if (!email) {
        setMessage("Enter an email address first.");
        emailInput?.focus();
        return;
      }
      setControlsDisabled(true);
      setMessage("Adding admin...");
      try {
        const response = await fetch("/api/admin/admins/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        const payload = await window.AuthExpired.parseOkJson(
          response,
          "Admin could not be added",
          reviewErrorFromPayload,
        );
        if (emailInput) emailInput.value = "";
        render(payload);
        setMessage(`Added ${email}.`);
      } catch (error) {
        setMessage(error.message || "Admin could not be added");
      } finally {
        setControlsDisabled(false);
      }
    }

    async function removeAdmin(email) {
      if (!email) return;
      setControlsDisabled(true);
      setMessage(`Removing ${email}...`);
      try {
        const response = await fetch("/api/admin/admins", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        const payload = await window.AuthExpired.parseOkJson(
          response,
          "Admin could not be removed",
          reviewErrorFromPayload,
        );
        render(payload);
        setMessage(`Removed ${email}.`);
      } catch (error) {
        // Reject path (e.g. 409 lockout / immutable env root): reload the
        // authoritative list so the row that was NOT removed stays visible, THEN
        // surface the server's reason inline (load() resets the message, so the
        // error is set last to win).
        const reason = error.message || "Admin could not be removed";
        await load();
        setMessage(reason);
      } finally {
        setControlsDisabled(false);
      }
    }

    function render(payload = {}) {
      const envRoots = Array.isArray(payload.env_root_admins) ? payload.env_root_admins : [];
      const persisted = Array.isArray(payload.persisted_admins) ? payload.persisted_admins : [];
      const total = envRoots.length + persisted.length;
      setOverall(total === 1 ? "1 admin" : `${total} admins`, "ready");
      setControlsDisabled(false);
      renderEnvRoots(envRoots);
      renderPersisted(persisted);
      setMessage("Add an admin by email, or remove a persisted admin.");
    }

    function renderEnvRoots(envRoots) {
      if (!envRootsList) return;
      if (!envRoots.length) {
        envRootsList.innerHTML = `<li class="admin-access-empty">No environment admins configured.</li>`;
        return;
      }
      envRootsList.innerHTML = envRoots
        .map(
          (id) => `
            <li class="admin-access-row">
              <span class="admin-access-email">${esc(id)}</span>
              <span class="admin-access-badge" title="Set in NDA_ADMIN_USERS; managed in the environment.">Bootstrap</span>
            </li>`,
        )
        .join("");
    }

    function renderPersisted(persisted) {
      if (!persistedList) return;
      if (!persisted.length) {
        persistedList.innerHTML = `<li class="admin-access-empty">No admins added in-app yet.</li>`;
        return;
      }
      persistedList.innerHTML = persisted
        .map((entry) => {
          const email = String(entry?.email || "");
          const addedBy = String(entry?.added_by || "");
          const addedAt = formatAddedAt(entry?.added_at);
          let meta = addedBy ? `Added by ${esc(addedBy)}` : "Added in-app";
          // addedAt is generated from numeric date pieces (safe); email/actor
          // stay escaped above. Omit the separator+date for older entries with
          // no parseable added_at (no "Invalid Date").
          if (addedAt) meta += ` &middot; ${addedAt}`;
          return `
            <li class="admin-access-row">
              <span class="admin-access-email">${esc(email)}</span>
              <span class="admin-access-meta">${meta}</span>
              <button class="link-button admin-access-remove" type="button" data-admin-remove="${esc(email)}">Remove</button>
            </li>`;
        })
        .join("");
    }

    function setControlsDisabled(disabled) {
      if (emailInput) emailInput.disabled = disabled;
      if (addButton) addButton.disabled = disabled;
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

function createAdminAccessController(options) {
  return AdminAccessView.createController(options);
}

// CommonJS export for the Node test harness (a no-op in the browser, where this
// file is loaded as a classic script and `module` is undefined).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminAccessView, createAdminAccessController };
}
