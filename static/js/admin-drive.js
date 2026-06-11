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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Drive status could not load");
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
          const payload = await response.json();
          if (!response.ok) throw reviewErrorFromPayload(payload, "Drive disconnect failed");
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
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Drive folder could not save");
        applyDriveSettings(payload.drive || {});
        setFact("folder-message", folderId
          ? "NDAs root folder saved. Per-matter subfolders are created inside it."
          : "Cleared the root folder. An \"NDAs\" folder is created in My Drive.");
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        setFact("folder-message", error.message || "Drive folder could not save");
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
      const overallLabel = connected ? "Connected" : signedIn && needsConnect ? "Needs Drive access" : "Not connected";
      // The toggle now reflects the connection itself (On = connected).
      setOverall(overallLabel, connected ? "ready" : "blocked");
      renderToggle(connected);
      renderConnect(status);
      renderFolderForm(status.folder || null);
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
      const statusLabel = signedIn && needsConnect ? "Needs Drive access" : "Needs setup";
      const accountLabel = status.account || (signedIn ? "Signed in Google session" : "Not connected");
      const nextStep = signedIn && needsConnect
        ? "Turn the Drive toggle on to grant Drive access for this signed-in account."
        : "Turn the Drive toggle on to connect a Google account.";
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
              <dd>${escapeHtml(nextStep)}</dd>
            </div>
            <div>
              <dt>Token</dt>
              <dd>${escapeHtml(signedIn && needsConnect ? "Drive token needed" : "No Drive token")}</dd>
            </div>
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

    return { load };
  }

  return { createController };
})();

function createAdminDriveController(options) {
  return AdminDriveView.createController(options);
}
