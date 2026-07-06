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
    signOutModal,
    signOutModalClose,
    signOutModalStatus,
    signOutThisDeviceButton,
    signOutAllDevicesButton,
    signOutCancelButton,
    accountToggle,
    accountMenu,
    avatarNode,
    avatarImage,
    avatarInitial,
    menuGreeting,
    menuStatus,
    menuAvatarImage,
    menuAvatarInitial,
    greetingNode,
    reviewErrorFromPayload,
    onGmailStatus,
    onSyncComplete,
  }) {
    let authStatus = null;
    let gmailStatus = null;
    let deploymentStatus = null;
    let greetingHelper = null;
    let menuOpen = false;
    let signOutBusy = false;
    let signOutPreviousFocus = null;
    const api = RepositoryApi.create({ reviewErrorFromPayload });

    // Load the greeting name-resolution helper once; re-render the greeting when
    // it arrives so the hero updates as soon as identity data is available.
    if (greetingNode) {
      import("./modules/greeting.mjs?v=20260605a")
        .then((module) => { greetingHelper = module; renderGreeting(); })
        .catch(() => {});
    }

    // Set the dashboard hero greeting from the best available name source.
    function renderGreeting() {
      if (!greetingNode || !greetingHelper) return;
      greetingNode.textContent = greetingHelper.dashboardGreeting({
        user: authStatus?.user || null,
        gmailStatus,
      });
    }

    loginLink?.addEventListener("click", () => {
      const href = authStatus?.login_url || "/auth/google/start";
      loginLink.href = `${href}${href.includes("?") ? "&" : "?"}next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    });
    logoutButton?.addEventListener("click", openSignOutDialog);
    signOutModalClose?.addEventListener("click", () => closeSignOutDialog());
    signOutCancelButton?.addEventListener("click", () => closeSignOutDialog());
    signOutModal?.addEventListener("click", (event) => {
      if (event.target === signOutModal && !signOutBusy) closeSignOutDialog();
    });
    signOutThisDeviceButton?.addEventListener("click", () => performLogout("/api/auth/logout"));
    signOutAllDevicesButton?.addEventListener("click", () => performLogout("/api/auth/logout-all"));
    connectButton?.addEventListener("click", () => {
      const href = gmailStatus?.connect_url || gmailStatus?.inbound?.connect_url || "/auth/gmail/start";
      window.location.href = withParams(href, { next: window.location.pathname + window.location.search, role: "all" });
    });
    disconnectButton?.addEventListener("click", disconnectGmail);
    syncButton?.addEventListener("click", syncGmail);
    accountToggle?.addEventListener("click", () => setMenuOpen(!menuOpen));
    document.addEventListener("click", (event) => {
      if (!menuOpen || !root) return;
      if (root.contains(event.target)) return;
      setMenuOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (isSignOutDialogOpen()) {
        if (signOutBusy) return;
        event.preventDefault();
        closeSignOutDialog();
        return;
      }
      setMenuOpen(false);
    });
    // Focus trap: keep Tab within the dialog while it is open.
    signOutModal?.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || !isSignOutDialogOpen()) return;
      const focusable = signOutFocusableNodes();
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !signOutModal.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    });

    async function load() {
      if (!root) return;
      render();
      // Deployment status is an admin-only endpoint (it surfaces operational/infra
      // detail -- auth/OAuth config, disk + memory headroom, which integrations are
      // configured, boot counts). It is loaded on demand from the admin health
      // section (see refreshDeploymentStatus), NOT here, so a non-admin authenticated
      // user never hits the admin-only 403 on normal app load.
      await Promise.all([loadAuthStatus(), loadGmailStatus()]);
      render();
    }

    async function refreshDeploymentStatus() {
      if (!root) return;
      await loadDeploymentStatus();
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

    function isSignOutDialogOpen() {
      return Boolean(signOutModal && !signOutModal.hidden);
    }

    function signOutFocusableNodes() {
      if (!signOutModal) return [];
      return Array.from(
        signOutModal.querySelectorAll(
          'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((node) => node instanceof HTMLElement && !node.hidden);
    }

    function setSignOutStatus(message) {
      if (!signOutModalStatus) return;
      signOutModalStatus.textContent = message || "";
    }

    function openSignOutDialog() {
      // No dialog wired (defensive) -- fall back to the previous direct behaviour.
      if (!signOutModal) {
        performLogout("/api/auth/logout");
        return;
      }
      setMenuOpen(false);
      setSignOutStatus("");
      signOutPreviousFocus = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      signOutModal.hidden = false;
      document.body.classList.add("modal-open");
      window.setTimeout(() => signOutThisDeviceButton?.focus?.(), 0);
    }

    function closeSignOutDialog({ restoreFocus = true } = {}) {
      if (!signOutModal) return;
      signOutModal.hidden = true;
      document.body.classList.remove("modal-open");
      setSignOutStatus("");
      if (restoreFocus) {
        const focusTarget = signOutPreviousFocus?.isConnected ? signOutPreviousFocus : logoutButton;
        focusTarget?.focus?.();
      }
      signOutPreviousFocus = null;
    }

    async function performLogout(url) {
      if (signOutBusy) return;
      signOutBusy = true;
      setBusy(logoutButton, true);
      // The choice buttons hold child <span>s, so disable them directly rather
      // than via setBusy (which rewrites textContent and would flatten them).
      setChoiceDisabled(signOutThisDeviceButton, true);
      setChoiceDisabled(signOutAllDevicesButton, true);
      setSignOutStatus("Signing out...");
      try {
        const response = await fetch(url, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Sign out failed");
        // Success: mirror the original logout behaviour exactly.
        window.location.reload();
      } catch (error) {
        const message = error.message || "Sign out failed";
        setSignOutStatus(message);
        setWarning(message);
      } finally {
        signOutBusy = false;
        setBusy(logoutButton, false);
        setChoiceDisabled(signOutThisDeviceButton, false);
        setChoiceDisabled(signOutAllDevicesButton, false);
      }
    }

    function setChoiceDisabled(button, disabled) {
      if (!button) return;
      button.disabled = Boolean(disabled);
      button.setAttribute("aria-busy", disabled ? "true" : "false");
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
      const greetingName = greetingNameForUser(user, gmailStatus);

      if (userNode) {
        const firstName = greetingName || "there";
        userNode.textContent = authStatus === null
          ? "Checking account"
          : authenticated
          ? `Hi, ${firstName}!`
          : authStatus?.google_oauth_configured
          ? "Sign in required"
          : "Local session";
        if (menuGreeting) {
          menuGreeting.textContent = greetingName ? `Hi, ${greetingName}!` : "Hi!";
        }
      }
      if (gmailNode) {
        const gmailLabel = gmailStatus === null
          ? "Checking Gmail"
          : canUseUserGmail
          ? ready
            ? `Gmail: ${inbound.email || outbound.email || "connected"}`
            : setupRequired
            ? "Gmail needs connection"
            : "Gmail status unavailable"
          : legacyGmailLabel(gmailStatus);
        gmailNode.textContent = gmailLabel;
        if (menuStatus) menuStatus.textContent = gmailLabel;
      }
      renderAvatar(user, gmailStatus);

      toggleHidden(loginLink, authenticated || !authStatus?.login_url);
      toggleHidden(logoutButton, authStatus === null);
      toggleHidden(connectButton, !canUseUserGmail || ready);
      toggleHidden(disconnectButton, !canUseUserGmail || (!inbound.token?.configured && !outbound.token?.configured));
      toggleHidden(syncButton, !canUseUserGmail || !inboundOnlyReady);
      setWarning(deploymentWarning() || authStatus?.error || "");
      if (accountToggle) {
        accountToggle.disabled = authStatus === null && gmailStatus === null;
        accountToggle.setAttribute("aria-expanded", menuOpen ? "true" : "false");
        const labelName = userNode?.textContent || "Account";
        accountToggle.setAttribute("aria-label", `${labelName}. Open account menu.`);
      }
      renderMenuVisibility();
      renderGreeting();
    }

    function setMenuOpen(open) {
      menuOpen = Boolean(open);
      renderMenuVisibility();
    }

    function renderMenuVisibility() {
      toggleHidden(accountMenu, !menuOpen);
      accountToggle?.setAttribute("aria-expanded", menuOpen ? "true" : "false");
    }

    function renderAvatar(user, status) {
      if (!avatarNode) return;
      const picture = profilePictureForUser(user, status);
      const initial = (greetingNameForUser(user, status) || "Account").slice(0, 1).toUpperCase() || "A";
      if (avatarInitial) avatarInitial.textContent = initial;
      showAvatarImage(avatarImage, avatarInitial, picture);
      if (menuAvatarInitial) menuAvatarInitial.textContent = initial;
      showAvatarImage(menuAvatarImage, menuAvatarInitial, picture);
    }

    function showAvatarFallback(imageNode, initialNode) {
      if (!imageNode) return;
      imageNode.removeAttribute("src");
      imageNode.hidden = true;
      if (initialNode) initialNode.hidden = false;
    }

    function showAvatarImage(imageNode, initialNode, picture) {
      if (!imageNode) return;
      if (picture) {
        imageNode.onerror = () => showAvatarFallback(imageNode, initialNode);
        imageNode.src = picture;
        imageNode.hidden = false;
        if (initialNode) initialNode.hidden = true;
        return;
      }
      imageNode.onerror = null;
      showAvatarFallback(imageNode, initialNode);
    }

    function firstNameForUser(user, status) {
      return greetingNameForUser(user, status) || "there";
    }

    function greetingNameForUser(user, status) {
      const name = firstNameFromDisplayName(firstAvailableText(
        user?.given_name,
        user?.name,
        status?.profile?.given_name,
        status?.profile?.name,
        status?.profile?.display_name,
        status?.user?.given_name,
        status?.user?.name,
        status?.inbound?.profile?.given_name,
        status?.inbound?.profile?.name,
        status?.inbound?.profile?.display_name,
        status?.outbound?.profile?.given_name,
        status?.outbound?.profile?.name,
        status?.outbound?.profile?.display_name,
      ), { email: user?.email, id: user?.id });
      if (name) return name;
      return firstNameFromEmail(firstAvailableText(
        user?.email,
        status?.profile?.email,
        status?.profile?.emailAddress,
        status?.user?.email,
        status?.inbound?.profile?.email,
        status?.inbound?.profile?.emailAddress,
        status?.inbound?.email,
        status?.outbound?.profile?.email,
        status?.outbound?.profile?.emailAddress,
        status?.outbound?.email,
      ));
    }

    function profilePictureForUser(user, status) {
      return firstAvailableText(
        user?.picture,
        user?.avatar_url,
        user?.photo_url,
        status?.profile?.picture,
        status?.profile?.avatar_url,
        status?.profile?.photo_url,
        status?.user?.picture,
        status?.user?.avatar_url,
        status?.user?.photo_url,
        status?.inbound?.profile?.picture,
        status?.inbound?.profile?.avatar_url,
        status?.inbound?.profile?.photo_url,
        status?.outbound?.profile?.picture,
        status?.outbound?.profile?.avatar_url,
        status?.outbound?.profile?.photo_url,
      );
    }

    function firstAvailableText(...values) {
      for (const value of values) {
        const text = String(value || "").trim();
        if (text) return text;
      }
      return "";
    }

    function firstNameFromDisplayName(name, { email, id } = {}) {
      const text = String(name || "").trim();
      if (!text || text.includes("@")) return "";
      const lower = text.toLowerCase();
      if (email && lower === String(email).trim().toLowerCase()) return "";
      if (id && lower === String(id).trim().toLowerCase()) return "";
      const first = text.split(/\s+/)[0] || "";
      return titleCaseToken(first);
    }

    function firstNameFromEmail(email) {
      const text = String(email || "").trim().toLowerCase();
      if (text.includes("@")) {
        const local = text.split("@")[0].split("+")[0];
        const first = local.split(/[.\-_]/).filter(Boolean)[0] || local;
        if (!first || first.length < 2 || /^\d+$/.test(first)) return "";
        return titleCaseToken(first);
      }
      return "";
    }

    function titleCaseToken(token) {
      return String(token || "")
        .toLowerCase()
        .replace(/(^|[-'])([a-z])/g, (_, sep, ch) => sep + ch.toUpperCase());
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

    return { load, refreshDeploymentStatus };
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
