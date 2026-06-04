const AuthSessionView = (() => {
  function createController({
    state,
    root,
    userNode,
    gmailNode,
    warningNode,
    loginLink,
    logoutButton,
    connectButton,
    syncButton,
    disconnectButton,
    reviewErrorFromPayload,
    onGmailStatus,
    onSyncComplete,
  }) {
    let authStatus = null;
    let gmailStatus = null;
    let deploymentStatus = null;
    const api = RepositoryApi.create({ reviewErrorFromPayload });

    loginLink?.addEventListener("click", () => {
      const href = authStatus?.login_url || "/auth/google/start";
      loginLink.href = `${href}${href.includes("?") ? "&" : "?"}next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    });
    logoutButton?.addEventListener("click", logout);
    connectButton?.addEventListener("click", () => {
      const href = gmailStatus?.connect_url || gmailStatus?.inbound?.connect_url || "/auth/gmail/start";
      window.location.href = withParams(href, { next: window.location.pathname + window.location.search, role: "all" });
    });
    disconnectButton?.addEventListener("click", disconnectGmail);
    syncButton?.addEventListener("click", syncGmail);

    async function load() {
      if (!root) return;
      render();
      await Promise.all([loadAuthStatus(), loadGmailStatus(), loadDeploymentStatus()]);
      render();
    }

    async function loadAuthStatus() {
      try {
        const response = await fetch("/api/auth/status");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Auth status could not load");
        authStatus = payload;
      } catch (error) {
        authStatus = { authenticated: false, error: error.message || "Auth status unavailable" };
      }
    }

    async function loadGmailStatus() {
      try {
        gmailStatus = await api.loadGmailStatus();
        state.gmailStatus = gmailStatus;
        if (typeof onGmailStatus === "function") onGmailStatus(gmailStatus);
      } catch (error) {
        gmailStatus = {
          inbound: { ready: false, error: error.message || "Gmail status unavailable" },
          outbound: { ready: false, error: error.message || "Gmail status unavailable" },
        };
      }
    }

    async function loadDeploymentStatus() {
      try {
        const response = await fetch("/api/deployment/status");
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Deployment status could not load");
        deploymentStatus = payload.deployment || null;
      } catch (_error) {
        deploymentStatus = null;
      }
    }

    async function logout() {
      setBusy(logoutButton, true);
      try {
        const response = await fetch("/api/auth/logout", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Sign out failed");
        window.location.reload();
      } catch (error) {
        setWarning(error.message || "Sign out failed");
      } finally {
        setBusy(logoutButton, false);
      }
    }

    async function disconnectGmail() {
      setBusy(disconnectButton, true);
      try {
        const response = await fetch("/api/gmail/disconnect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: "all" }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail disconnect failed");
        gmailStatus = payload.gmail || gmailStatus || {};
        state.gmailStatus = gmailStatus;
        if (typeof onGmailStatus === "function") onGmailStatus(gmailStatus);
        render();
      } catch (error) {
        setWarning(error.message || "Gmail disconnect failed");
      } finally {
        setBusy(disconnectButton, false);
      }
    }

    async function syncGmail() {
      setBusy(syncButton, true, "Syncing");
      try {
        const payload = await api.syncGmail({ limit: 25 });
        gmailStatus = payload.gmail || gmailStatus || {};
        state.gmailStatus = gmailStatus;
        if (typeof onGmailStatus === "function") onGmailStatus(gmailStatus);
        if (typeof onSyncComplete === "function") onSyncComplete(payload.result || {});
        render();
      } catch (error) {
        setWarning(error.message || "Gmail sync failed");
      } finally {
        setBusy(syncButton, false);
      }
    }

    function render() {
      if (!root) return;
      const user = authStatus?.user || null;
      const authenticated = authStatus?.authenticated === true || Boolean(user?.id);
      const inbound = gmailStatus?.inbound || {};
      const outbound = gmailStatus?.outbound || {};
      const canUseUserGmail = gmailStatus?.user_scoped === true;
      const ready = inbound.ready === true && outbound.ready === true;
      const inboundOnlyReady = inbound.ready === true;
      const setupRequired = canUseUserGmail && (!inbound.ready || !outbound.ready);

      if (userNode) {
        userNode.textContent = authStatus === null
          ? "Checking account"
          : authenticated
          ? `Signed in: ${user.email || user.name || user.id || "Google user"}`
          : authStatus?.google_oauth_configured
          ? "Sign in required"
          : "Local session";
      }
      if (gmailNode) {
        gmailNode.textContent = gmailStatus === null
          ? "Checking Gmail"
          : canUseUserGmail
          ? ready
            ? `Gmail connected: ${inbound.email || outbound.email || "this user"}`
            : setupRequired
            ? "Gmail needs connection"
            : "Gmail status unavailable"
          : legacyGmailLabel(gmailStatus);
      }

      toggleHidden(loginLink, authenticated || !authStatus?.login_url);
      toggleHidden(logoutButton, !authenticated);
      toggleHidden(connectButton, !canUseUserGmail || ready);
      toggleHidden(disconnectButton, !canUseUserGmail || (!inbound.token?.configured && !outbound.token?.configured));
      toggleHidden(syncButton, !canUseUserGmail || !inboundOnlyReady);
      setWarning(deploymentWarning() || authStatus?.error || "");
    }

    function deploymentWarning() {
      if (!deploymentStatus || deploymentStatus.status === "ok") return "";
      const failed = Array.isArray(deploymentStatus.checks)
        ? deploymentStatus.checks.find((check) => check && check.ok === false)
        : null;
      return failed?.message || "Deployment needs attention";
    }

    function setWarning(message) {
      if (!warningNode) return;
      warningNode.textContent = message;
      toggleHidden(warningNode, !message);
      root?.classList.toggle("has-warning", Boolean(message));
    }

    return { load };
  }

  function legacyGmailLabel(gmailStatus) {
    const inbound = gmailStatus?.inbound || {};
    const outbound = gmailStatus?.outbound || {};
    if (inbound.ready && outbound.ready) return "Shared Gmail configured";
    if (inbound.error || outbound.error) return "Gmail setup required";
    return "Gmail status loading";
  }

  function setBusy(button, busy, label = "") {
    if (!button) return;
    if (busy) {
      button.dataset.idleLabel = button.textContent || "";
      if (label) button.textContent = label;
    } else if (button.dataset.idleLabel) {
      button.textContent = button.dataset.idleLabel;
    }
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function toggleHidden(node, hidden) {
    if (node) node.hidden = Boolean(hidden);
  }

  function withParams(url, params) {
    const target = new URL(url, window.location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (!target.searchParams.has(key)) target.searchParams.set(key, value);
    });
    return `${target.pathname}${target.search}${target.hash}`;
  }

  return { createController };
})();

function createAuthSessionController(options) {
  return AuthSessionView.createController(options);
}
