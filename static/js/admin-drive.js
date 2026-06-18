const AdminDriveView = (() => {
  function createController({
    state,
    driveCard,
    driveFacts,
    driveOverall,
    driveRefreshButton,
    driveConnectPanel,
    driveEnabledToggle,
    driveFolderForm,
    driveFolderIdInput,
    driveFolderNameInput,
    driveFolderSaveButton,
    reviewErrorFromPayload,
  }) {
    driveRefreshButton?.addEventListener("click", load);
    driveEnabledToggle?.addEventListener("click", updateDriveEnabled);
    driveFolderForm?.addEventListener("submit", saveFolderSettings);

    async function load() {
      if (!driveCard) return;
      setOverall("Checking", "pending");
      try {
        const response = await fetch("/api/drive/status");
        const payload = await window.AuthExpired.parseOkJson(response, "Drive status could not load", reviewErrorFromPayload);
        renderDrive(payload);
      } catch (error) {
        renderError(error.message || "Drive status could not load");
      }
    }

    // The single Drive toggle IS the whole control: On launches the Google
    // connect flow, Off disconnects (removes the Drive token). A fresh connect
    // lands enabled, so there is nothing else to flip.
    async function updateDriveEnabled() {
      const connected = state.driveStatus?.connected === true;
      if (connected) {
        setToggleDisabled(true);
        setOverall("Disconnecting", "pending");
        try {
          const response = await fetch("/api/drive/disconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          const payload = await window.AuthExpired.parseOkJson(response, "Drive disconnect failed", reviewErrorFromPayload);
          await load();
        } catch (error) {
          setOverall(error.message || "Disconnect failed", "blocked");
          renderToggle(state.driveStatus?.connected === true);
        } finally {
          setToggleDisabled(false);
        }
        return;
      }
      // On = connect: hand off to the Google consent screen, returning here after.
      const url = connectUrl(state.driveStatus || {});
      const separator = url.includes("?") ? "&" : "?";
      window.location.href = `${url}${separator}next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    }

    async function saveFolderSettings(event) {
      event.preventDefault();
      const folderId = driveFolderIdInput?.value.trim() || "";
      const folderName = driveFolderNameInput?.value.trim() || "";
      setFolderControlsDisabled(true);
      setOverall("Saving", "pending");
      setFact("folder-message", "Saving Drive folder settings...");
      try {
        const requestPayload = { folder_id: folderId };
        if (folderName) requestPayload.folder_name = folderName;
        const response = await fetch("/api/admin/drive-settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestPayload),
        });
        // parseOkJson rejects with the server's {error} message on a non-2xx, so the
        // success copy + persisted-settings render below are reached ONLY when the
        // backend actually validated the folder (exists + is a folder + writable).
        const payload = await window.AuthExpired.parseOkJson(response, "Drive folder could not save", reviewErrorFromPayload);
        applyDriveSettings(payload.drive || {});
        setOverall("Saved", "ready");
        setFact("folder-message", folderId
          ? "NDAs root folder verified and saved. Per-matter subfolders are created inside it."
          : "Cleared the root folder. An \"NDAs\" folder is created in My Drive.");
      } catch (error) {
        // The 400 carries the specific reason (folder not found / not a folder / no
        // write permission / Drive auth expired). Surface it inline on the Save action
        // so the admin sees exactly why nothing was saved.
        const message = (error && error.message) || "Drive folder could not save";
        setOverall("Not saved", "blocked");
        setFact("folder-message", message);
      } finally {
        setFolderControlsDisabled(false);
      }
    }

    // POST /api/admin/drive-settings returns only {enabled, folder_id, folder_name}.
    // Fold those into the cached status so the toggle/facts stay consistent
    // without a second status round-trip.
    function applyDriveSettings(drive) {
      const previous = state.driveStatus || {};
      const folderId = drive.folder_id != null ? String(drive.folder_id) : (previous.folder?.id || "");
      const folderName = drive.folder_name != null ? String(drive.folder_name) : (previous.folder?.name || "");
      renderDrive({
        ...previous,
        enabled: drive.enabled === true,
        folder: folderId || folderName ? { id: folderId, name: folderName } : null,
      });
    }

    function renderDrive(status = {}) {
      state.driveStatus = status;
      const connected = status.connected === true;
      const signedIn = status.signed_in === true;
      const needsConnect = status.needs_connect === true || (!connected && Boolean(status.connect_url));
      const setupState = driveSetupState(status);
      const overallLabel = connected ? "Connected" : signedIn && needsConnect ? "Needs Drive access" : setupState.statusLabel;
      // The toggle now reflects the connection itself (On = connected).
      setOverall(overallLabel, connected ? "ready" : "blocked");
      renderToggle(connected);
      renderToggleIntent(status);
      renderConnect(status);
      renderFolderForm(status.folder || null);
      renderFilingBanner(status);
      setFact("connection", overallLabel);
      setFact("account", status.account || (connected ? "Connected account" : signedIn ? "Signed in Google session" : "No account connected"));
      setFact("folder", folderLabel(status.folder));
      setFact("enabled-copy", connected ? "On" : "Off");
    }

    function renderConnect(status) {
      if (!driveConnectPanel) return;
      const connected = status.connected === true;
      if (connected) {
        driveConnectPanel.innerHTML = `
          <div class="integration-connection-row ready">
            <div class="integration-connection-top">
              <strong>Google Drive</strong>
              <span>Connected</span>
            </div>
            <dl>
              <div>
                <dt>Account</dt>
                <dd>${escapeHtml(status.account || "Connected account")}</dd>
              </div>
              <div>
                <dt>NDAs root folder</dt>
                <dd>${escapeHtml(folderLabel(status.folder))}</dd>
              </div>
            </dl>
          </div>
        `;
        return;
      }
      const signedIn = status.signed_in === true;
      const needsConnect = status.needs_connect === true || Boolean(status.connect_url);
      const setupState = driveSetupState(status);
      const statusLabel = signedIn && needsConnect ? "Needs Drive access" : setupState.statusLabel;
      const accountLabel = status.account || (signedIn ? "Signed in Google session" : "Not connected");
      const scopeCopy = scopeLabel(status);
      driveConnectPanel.innerHTML = `
        <div class="integration-connection-row blocked">
          <div class="integration-connection-top">
            <strong>Google Drive</strong>
            <span>${escapeHtml(statusLabel)}</span>
          </div>
          <dl>
            <div>
              <dt>Account</dt>
              <dd>${escapeHtml(accountLabel)}</dd>
            </div>
            <div>
              <dt>Next step</dt>
              <dd>${escapeHtml(setupState.nextStep)}</dd>
            </div>
            <div>
              <dt>Token</dt>
              <dd>${escapeHtml(tokenLabel(status))}</dd>
            </div>
            ${scopeCopy ? `
              <div>
                <dt>Scope</dt>
                <dd>${escapeHtml(scopeCopy)}</dd>
              </div>
            ` : ""}
          </dl>
        </div>
      `;
    }

    function renderFolderForm(folder) {
      if (driveFolderIdInput) driveFolderIdInput.value = folder?.id || "";
      if (driveFolderNameInput) driveFolderNameInput.value = folder?.name || "";
    }

    function renderError(message) {
      setOverall("Unavailable", "blocked");
      setFact("connection", message);
      setFact("account", "Unknown");
      setFact("folder", "Unknown");
      setFact("enabled-copy", "Unknown");
      setFact("filing-location", "Currently filing NDAs in: Unknown");
      setFact("folder-message", message);
      renderConnect(state.driveStatus || {});
      renderToggle(state.driveStatus?.connected === true);
    }

    function renderToggle(enabled) {
      if (!driveEnabledToggle) return;
      driveEnabledToggle.setAttribute("aria-checked", enabled ? "true" : "false");
      driveEnabledToggle.classList.toggle("on", enabled);
      driveEnabledToggle.classList.toggle("off", !enabled);
    }

    function renderToggleIntent(status = {}) {
      if (!driveEnabledToggle) return;
      const label = status.connected === true
        ? "Disconnect Google Drive"
        : driveSetupState(status).actionLabel;
      driveEnabledToggle.setAttribute("aria-label", label);
      driveEnabledToggle.title = label;
    }

    function setToggleDisabled(disabled) {
      if (driveEnabledToggle) driveEnabledToggle.disabled = disabled;
    }

    function setFolderControlsDisabled(disabled) {
      if (driveFolderIdInput) driveFolderIdInput.disabled = disabled;
      if (driveFolderNameInput) driveFolderNameInput.disabled = disabled;
      if (driveFolderSaveButton) driveFolderSaveButton.disabled = disabled;
    }

    function setOverall(label, tone) {
      if (!driveOverall) return;
      driveOverall.textContent = label;
      driveOverall.classList.toggle("ready", tone === "ready");
      driveOverall.classList.toggle("blocked", tone === "blocked");
      driveOverall.classList.toggle("pending", tone === "pending");
    }

    function setFact(key, value) {
      const node = driveCard?.querySelector(`[data-admin-drive="${key}"]`) || driveFacts?.querySelector(`[data-admin-drive="${key}"]`);
      if (node) node.textContent = value;
    }

    // Always-visible confirmation of the REAL filing destination, so a blank or
    // cleared root folder no longer silently falls back with no on-screen signal.
    // Prefers the server-resolved filing_location.label (which resolves the
    // configured folder's real name); falls back to a label derived from the
    // cached folder when the field is absent (e.g. a settings-save refresh).
    function renderFilingBanner(status = {}) {
      setFact("filing-location", `Currently filing NDAs in: ${filingLocationLabel(status)}`);
    }

    function filingLocationLabel(status = {}) {
      const location = status.filing_location;
      if (location && typeof location === "object") {
        const label = String(location.label || "").trim();
        if (label) return label;
      }
      const folder = status.folder;
      if (folder) {
        const name = String(folder.name || "").trim();
        const id = String(folder.id || "").trim();
        const display = name || id;
        if (display) return `${display} / NDAs`;
      }
      return "My Drive / NDAs (default location)";
    }

    function folderLabel(folder) {
      if (!folder) return "My Drive / NDAs (no root folder set)";
      const name = String(folder.name || "").trim();
      const id = String(folder.id || "").trim();
      if (name && id) return `${name} (${id})`;
      return name || id || "My Drive / NDAs (no root folder set)";
    }

    function connectUrl(status) {
      // Drive is part of the unified Google connection now, so connect through the
      // shared /auth/gmail/start (role=all) flow: it uses the already-registered
      // Gmail callback (no /auth/drive/callback to register), grants Gmail + Drive
      // in one consent, and shows the account chooser. Avoids redirect_uri_mismatch.
      return String(status?.connect_url || "/auth/gmail/start");
    }

    function driveSetupState(status = {}) {
      const setup = status.setup || {};
      const recovery = status.recovery || {};
      const state = recovery.state || setup.state || "";
      const message = String(recovery.message || setup.message || "").trim();
      if (status.connected === true) {
        return {
          actionLabel: "Disconnect Google Drive",
          nextStep: "Drive is connected.",
          statusLabel: "Connected",
        };
      }
      if (
        state === "missing_oauth_config"
        || status.google_oauth_configured === false
        || status.oauth_configured === false
        || setup.google_oauth_configured === false
      ) {
        return {
          actionLabel: "Google OAuth is not configured",
          nextStep: message || "Configure the Google OAuth client ID and secret, then refresh this page.",
          statusLabel: "Needs OAuth config",
        };
      }
      if (
        state === "sign_in_required"
        || (status.user_scoped === true && status.signed_in === false)
      ) {
        return {
          actionLabel: "Sign in with Google to connect Drive",
          nextStep: message || "Turn the Drive toggle on to sign in with Google and grant Drive access.",
          statusLabel: "Needs Google sign-in",
        };
      }
      if (state === "missing_token") {
        return {
          actionLabel: "Connect Google Drive",
          nextStep: message || "Turn the Drive toggle on to create a Drive token for this account.",
          statusLabel: "Needs Drive token",
        };
      }
      if (state === "missing_scope" || scopeLabel(status)) {
        return {
          actionLabel: "Reconnect Google Drive and approve Drive scope",
          nextStep: message || "Turn the Drive toggle on to reconnect and approve the required Drive scope.",
          statusLabel: "Needs Drive scope",
        };
      }
      if (status.signed_in === true && (status.needs_connect === true || status.connect_url)) {
        return {
          actionLabel: "Grant Google Drive access",
          nextStep: message || "Turn the Drive toggle on to grant Drive access for this signed-in account.",
          statusLabel: "Needs Drive access",
        };
      }
      if (status.enabled === false) {
        return {
          actionLabel: "Connect Google Drive uploads",
          nextStep: "Turn the Drive toggle on to connect Google Drive uploads.",
          statusLabel: "Needs setup",
        };
      }
      return {
        actionLabel: "Connect Google Drive",
        nextStep: "Turn the Drive toggle on to connect a Google account.",
        statusLabel: "Not connected",
      };
    }

    function tokenLabel(status = {}) {
      const token = status.token || {};
      const label = String(token.label || "").trim();
      if (token.source === "user_data" && token.configured === true) return `User Drive: ${label || status.account || "connected OAuth token"}`;
      if (token.source === "legacy_gmail_scope") return `Legacy Gmail token: ${label || "visible with Drive scope"}`;
      if (token.source === "legacy_gmail") return `Legacy Gmail token: ${label || "visible but Drive scope must be confirmed"}`;
      if (token.source === "missing" || token.configured === false) return `Missing: ${label || "Drive token"}`;
      if (status.signed_in === true && (status.needs_connect === true || status.connect_url)) return "Drive token needed";
      if (status.connected === true) return "Configured";
      return "No Drive token";
    }

    function scopeLabel(status = {}) {
      const scopes = [];
      if (Array.isArray(status.missing_scopes)) scopes.push(...status.missing_scopes);
      if (Array.isArray(status.token?.missing_scopes)) scopes.push(...status.token.missing_scopes);
      if (Array.isArray(status.token?.scope_status?.missing)) scopes.push(...status.token.scope_status.missing);
      if (Array.isArray(status.recovery?.scope_status?.missing)) scopes.push(...status.recovery.scope_status.missing);
      if (status.scope_status === "missing" || status.token?.scope_status === "missing") scopes.push("required Drive scope");
      if (status.token?.scope_status?.ok === false || status.recovery?.scope_status?.ok === false) scopes.push("required Drive scope");
      const unique = [...new Set(scopes.map((scope) => String(scope).trim()).filter(Boolean))];
      return unique.length ? `Missing: ${unique.join(", ")}` : "";
    }

    return { load };
  }

  return { createController };
})();

function createAdminDriveController(options) {
  return AdminDriveView.createController(options);
}
