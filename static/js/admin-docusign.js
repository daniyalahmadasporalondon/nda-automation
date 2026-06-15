// Admin -> DocuSign panel controller.
//
// Mirrors createAdminDriveController: a single connect/disconnect control plus a
// read-only connection summary, all driven by the DocuSign REST contract:
//   GET  /api/docusign/status      -> { connected, account? }
//   POST /api/docusign/connect     -> starts the real DocuSign OAuth flow
//   POST /api/docusign/disconnect  -> removes the connection
//
// Connect is a real OAuth redirect (exactly like the Drive/Gmail Connect
// buttons): when not connected the toggle hands the browser off to DocuSign's
// consent page; on return the page reloads and shows the live connection status
// (account label) from /api/docusign/status. When connected the toggle
// disconnects in place.
//
// The status-shaping decisions live in DocuSignModel.connectionView so the
// browser path is the exact path the frontend test exercises.
const AdminDocuSignView = (() => {
  function html(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") {
      return window.escapeHtml(value);
    }
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function model() {
    if (typeof window !== "undefined" && window.DocuSignModel) return window.DocuSignModel;
    if (typeof DocuSignModel !== "undefined") return DocuSignModel;
    return null;
  }

  function createController({
    state,
    docusignCard,
    docusignFacts,
    docusignOverall,
    docusignRefreshButton,
    docusignConnectPanel,
    docusignConnectToggle,
    reviewErrorFromPayload,
  }) {
    docusignRefreshButton?.addEventListener("click", load);
    docusignConnectToggle?.addEventListener("click", updateConnection);

    async function load() {
      if (!docusignCard) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/docusign/status");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "DocuSign status could not load");
        render(payload);
      } catch (error) {
        renderError(error.message || "DocuSign status could not load");
      }
    }

    // The single toggle IS the whole control. On = connect (hand off to the real
    // DocuSign OAuth consent screen, returning here after); Off = disconnect
    // (remove the connection). A fresh connect lands connected, so there is
    // nothing else to flip.
    async function updateConnection() {
      const connected = state.docusignStatus?.connected === true;
      if (connected) {
        setToggleDisabled(true);
        setOverall("Disconnecting", "pending");
        try {
          const response = await fetch("/api/docusign/disconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          const payload = await response.json();
          if (!response.ok) throw reviewErrorFromPayload(payload, "DocuSign disconnect failed");
          await load();
        } catch (error) {
          setOverall(error.message || "Disconnect failed", "blocked");
          renderToggle(state.docusignStatus?.connected === true);
        } finally {
          setToggleDisabled(false);
        }
        return;
      }
      // On = connect: start the real DocuSign OAuth flow. Prefer a server-provided
      // connect_url (the consent URL); otherwise POST the connect-start endpoint
      // and follow the redirect URL it returns. Either way we hand the browser
      // off to DocuSign's consent page, returning to this page afterwards.
      setToggleDisabled(true);
      setOverall("Connecting", "pending");
      try {
        const directUrl = String(state.docusignStatus?.connect_url || "").trim();
        if (directUrl) {
          window.location.href = withNext(directUrl);
          return;
        }
        const response = await fetch("/api/docusign/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "DocuSign connect failed");
        const redirectUrl = String(payload.connect_url || payload.redirect_url || payload.authorization_url || "").trim();
        if (redirectUrl) {
          window.location.href = withNext(redirectUrl);
          return;
        }
        // No redirect URL returned — the connect call already flipped the state
        // (e.g. server-side connect). Re-read status to reflect it.
        await load();
      } catch (error) {
        setOverall(error.message || "Connect failed", "blocked");
        renderToggle(state.docusignStatus?.connected === true);
      } finally {
        setToggleDisabled(false);
      }
    }

    function render(status = {}) {
      state.docusignStatus = status;
      const view = model()?.connectionView(status) || fallbackView(status);
      setOverall(view.statusLabel, view.tone);
      renderToggle(view.connected);
      renderToggleIntent(view);
      renderConnect(view);
      setFact("connection", view.statusLabel);
      setFact("account", view.account);
      setFact("enabled-copy", view.connected ? "On" : "Off");
    }

    function renderConnect(view) {
      if (!docusignConnectPanel) return;
      const tone = view.connected ? "ready" : "blocked";
      docusignConnectPanel.innerHTML = `
        <div class="integration-connection-row ${tone}">
          <div class="integration-connection-top">
            <strong>DocuSign</strong>
            <span>${html(view.statusLabel)}</span>
          </div>
          <dl>
            <div>
              <dt>Account</dt>
              <dd>${html(view.account)}</dd>
            </div>
            <div>
              <dt>Next step</dt>
              <dd>${html(view.connected
                ? "DocuSign is connected. Send finalised NDAs for signature from the review workstation."
                : "Turn the switch on to connect a DocuSign account via OAuth.")}</dd>
            </div>
          </dl>
        </div>
      `;
    }

    function renderError(message) {
      setOverall("Unavailable", "blocked");
      setFact("connection", message);
      setFact("account", "Unknown");
      setFact("enabled-copy", "Unknown");
      renderToggle(state.docusignStatus?.connected === true);
      if (docusignConnectPanel) {
        docusignConnectPanel.innerHTML = `
          <div class="integration-connection-row blocked">
            <div class="integration-connection-top">
              <strong>DocuSign</strong>
              <span>Unavailable</span>
            </div>
            <dl>
              <div>
                <dt>Status</dt>
                <dd>${html(message)}</dd>
              </div>
            </dl>
          </div>
        `;
      }
    }

    function renderToggle(enabled) {
      if (!docusignConnectToggle) return;
      docusignConnectToggle.setAttribute("aria-checked", enabled ? "true" : "false");
      docusignConnectToggle.classList.toggle("on", enabled);
      docusignConnectToggle.classList.toggle("off", !enabled);
    }

    function renderToggleIntent(view) {
      if (!docusignConnectToggle) return;
      docusignConnectToggle.setAttribute("aria-label", view.actionLabel);
      docusignConnectToggle.title = view.actionLabel;
    }

    function setToggleDisabled(disabled) {
      if (docusignConnectToggle) docusignConnectToggle.disabled = disabled;
    }

    function setOverall(label, tone) {
      if (!docusignOverall) return;
      docusignOverall.textContent = label;
      docusignOverall.classList.toggle("ready", tone === "ready");
      docusignOverall.classList.toggle("blocked", tone === "blocked");
      docusignOverall.classList.toggle("pending", tone === "pending");
    }

    function setFact(key, value) {
      const node = docusignCard?.querySelector(`[data-admin-docusign="${key}"]`)
        || docusignFacts?.querySelector(`[data-admin-docusign="${key}"]`);
      if (node) node.textContent = value;
    }

    function withNext(url) {
      try {
        const target = new URL(url, window.location.origin);
        if (!target.searchParams.has("next")) {
          target.searchParams.set("next", window.location.pathname + window.location.search);
        }
        return `${target.pathname}${target.search}${target.hash}`;
      } catch (error) {
        const separator = url.includes("?") ? "&" : "?";
        return `${url}${separator}next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
      }
    }

    // Fallback if the shared model module failed to load — keep the panel usable.
    function fallbackView(status = {}) {
      const connected = status?.connected === true;
      return {
        connected,
        account: connected ? "Connected account" : "No account connected",
        tone: connected ? "ready" : "blocked",
        statusLabel: connected ? "Connected" : "Not connected",
        actionLabel: connected ? "Disconnect DocuSign" : "Connect DocuSign",
      };
    }

    return { load };
  }

  return { createController };
})();

function createAdminDocuSignController(options) {
  return AdminDocuSignView.createController(options);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminDocuSignView, createAdminDocuSignController };
}
