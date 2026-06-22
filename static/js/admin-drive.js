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
    driveFolderSaveButton,
    // Name-first display layer. The raw id stays in driveFolderIdInput (the single
    // source of truth Save reads); these surface the friendly folder NAME and let a
    // power-user reveal the raw id field via "Edit ID". All optional: if the markup
    // is absent the field falls back to the plain raw-id behaviour.
    driveFolderDisplay,
    driveFolderDisplayName,
    driveFolderDisplayId,
    driveFolderIdRow,
    driveFolderEditIdButton,
    // Folder-picker controls ("Browse Drive"). All optional: if the modal markup
    // is absent the picker is simply inert and the paste-an-ID flow is unchanged.
    driveBrowseButton,
    // Second "Browse Drive" button living in the raw-id row (Edit-ID mode). Optional.
    driveBrowseButtonAlt,
    drivePickerBackdrop,
    drivePickerClose,
    drivePickerCancel,
    drivePickerSelect,
    drivePickerList,
    drivePickerBreadcrumb,
    // "← Back" goes up one level (pops the breadcrumb trail). Optional.
    drivePickerBack,
    drivePickerStatus,
    drivePickerSelection,
    // "+ New folder" controls inside the picker. All optional.
    drivePickerNewToggle,
    drivePickerNewRow,
    drivePickerNewInput,
    drivePickerNewCreate,
    drivePickerNewCancel,
    drivePickerNewError,
    reviewErrorFromPayload,
    // Injected from app.js -> notificationsController.notifySuccess so a finished
    // folder save flashes the ONE green success toast. No-op in the Node harness.
    notifySuccess,
  }) {
    driveRefreshButton?.addEventListener("click", load);
    driveEnabledToggle?.addEventListener("click", updateDriveEnabled);
    driveFolderForm?.addEventListener("submit", saveFolderSettings);

    // --- Drive folder picker state + wiring --------------------------------
    // Breadcrumb trail of folders we've drilled into, root-first. Each entry is
    // {id, name}. "My Drive" (id "root") is always the first crumb.
    let pickerTrail = [{ id: "root", name: "My Drive" }];
    let pickerSelected = null; // {id, name} of the highlighted folder, or null.
    // The manual "Root folder name" input was removed; the real folder name is
    // captured here when the admin picks/creates a folder in the browser, and
    // included as folder_name on save. The banner resolves it for display
    // regardless, so an empty value is harmless (it is simply omitted).
    let capturedFolderName = "";
    let capturedFolderId = ""; // the id capturedFolderName belongs to.

    driveBrowseButton?.addEventListener("click", openPicker);
    driveBrowseButtonAlt?.addEventListener("click", openPicker);
    driveFolderEditIdButton?.addEventListener("click", showFolderIdEditor);
    drivePickerClose?.addEventListener("click", closePicker);
    drivePickerCancel?.addEventListener("click", closePicker);
    drivePickerSelect?.addEventListener("click", confirmPickerSelection);
    drivePickerBack?.addEventListener("click", goUpOneLevel);
    drivePickerBackdrop?.addEventListener("click", (event) => {
      // Click on the dimmed backdrop (not the dialog itself) closes the picker.
      if (event.target === drivePickerBackdrop) closePicker();
    });

    // "+ New folder": reveal an inline name input, then POST a create request.
    drivePickerNewToggle?.addEventListener("click", openNewFolderInput);
    drivePickerNewCancel?.addEventListener("click", closeNewFolderInput);
    drivePickerNewCreate?.addEventListener("click", createFolder);

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
      // Only carry the captured name when it still matches the id in the field —
      // a hand-typed/edited id has no captured name, so we omit folder_name and
      // let the banner resolve the real name server-side.
      const folderName = folderId && folderId === capturedFolderId ? capturedFolderName.trim() : "";
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
        // SUCCESS: flash the transient green toast; the inline fact settles to a
        // neutral resting line that describes the persisted folder behaviour.
        const subtitle = folderId
          ? "Per-NDA subfolders are created inside it"
          : "An \"NDAs\" folder is created in My Drive";
        setFact("folder-message", folderId
          ? "Per-NDA subfolders are created inside the root folder."
          : "An \"NDAs\" folder is created in My Drive.");
        if (typeof notifySuccess === "function") {
          notifySuccess("Drive folder saved", subtitle);
        }
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

    // --- Folder picker ------------------------------------------------------
    function openPicker() {
      if (!drivePickerBackdrop) return;
      // Always start the browse at My Drive root. (We don't try to resolve the
      // pasted id back to a path — root is the predictable, single source.)
      pickerTrail = [{ id: "root", name: "My Drive" }];
      pickerSelected = null;
      drivePickerBackdrop.hidden = false;
      closeNewFolderInput();
      renderBreadcrumb();
      renderSelection();
      loadFolders("root");
    }

    function closePicker() {
      if (drivePickerBackdrop) drivePickerBackdrop.hidden = true;
    }

    function currentParentId() {
      return pickerTrail.length ? pickerTrail[pickerTrail.length - 1].id : "root";
    }

    async function loadFolders(parentId) {
      setPickerStatus("Loading folders...");
      if (drivePickerList) drivePickerList.innerHTML = "";
      pickerSelected = null;
      renderSelection();
      try {
        const response = await fetch(`/api/admin/drive-folders?parent=${encodeURIComponent(parentId)}`);
        const payload = await window.AuthExpired.parseOkJson(response, "Drive folders could not load", reviewErrorFromPayload);
        renderFolders(Array.isArray(payload.folders) ? payload.folders : []);
      } catch (error) {
        setPickerStatus(error.message || "Drive folders could not load");
      }
    }

    function renderFolders(folders) {
      if (!drivePickerList) return;
      drivePickerList.innerHTML = "";
      if (!folders.length) {
        setPickerStatus("No subfolders here. \"Use this folder\" selects the current folder.");
        return;
      }
      setPickerStatus("");
      for (const folder of folders) {
        const id = String(folder.id || "");
        const name = String(folder.name || "");
        if (!id) continue;
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "drive-picker-folder";
        const nameSpan = document.createElement("span");
        nameSpan.className = "drive-picker-folder-name";
        nameSpan.textContent = name || id;
        const openSpan = document.createElement("span");
        openSpan.className = "drive-picker-open";
        openSpan.textContent = "Open >";
        button.appendChild(nameSpan);
        button.appendChild(openSpan);
        // Single click = select (highlight); the "Open >" affordance drills in.
        button.addEventListener("click", (event) => {
          if (event.target === openSpan) {
            drillInto({ id, name });
          } else {
            selectFolder({ id, name }, button);
          }
        });
        openSpan.addEventListener("click", (event) => {
          event.stopPropagation();
          drillInto({ id, name });
        });
        li.appendChild(button);
        drivePickerList.appendChild(li);
      }
    }

    function selectFolder(folder, button) {
      pickerSelected = folder;
      if (drivePickerList) {
        for (const node of drivePickerList.querySelectorAll(".drive-picker-folder")) {
          node.classList.toggle("selected", node === button);
        }
      }
      renderSelection();
    }

    function drillInto(folder) {
      pickerTrail.push({ id: folder.id, name: folder.name || folder.id });
      renderBreadcrumb();
      loadFolders(folder.id);
    }

    function jumpToCrumb(index) {
      if (index < 0 || index >= pickerTrail.length) return;
      pickerTrail = pickerTrail.slice(0, index + 1);
      renderBreadcrumb();
      loadFolders(currentParentId());
    }

    // "← Back": go up one level by dropping the current (tail) crumb and
    // re-listing the parent. Identical navigation path to clicking the parent
    // breadcrumb crumb. A no-op at My Drive root (the Back button is disabled
    // there), so this is also a defensive guard.
    function goUpOneLevel() {
      if (pickerTrail.length <= 1) return;
      jumpToCrumb(pickerTrail.length - 2);
    }

    // The Back button is meaningful only once we've drilled past My Drive root.
    function renderBackButton() {
      if (!drivePickerBack) return;
      drivePickerBack.disabled = pickerTrail.length <= 1;
    }

    function renderBreadcrumb() {
      renderBackButton();
      if (!drivePickerBreadcrumb) return;
      drivePickerBreadcrumb.innerHTML = "";
      pickerTrail.forEach((crumb, index) => {
        if (index > 0) {
          const sep = document.createElement("span");
          sep.className = "drive-picker-crumb-sep";
          sep.textContent = "/";
          drivePickerBreadcrumb.appendChild(sep);
        }
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = crumb.name || crumb.id;
        const isLast = index === pickerTrail.length - 1;
        if (isLast) button.disabled = true;
        else button.addEventListener("click", () => jumpToCrumb(index));
        drivePickerBreadcrumb.appendChild(button);
      });
    }

    function renderSelection() {
      const target = pickerSelected || currentFolderAsSelection();
      if (drivePickerSelection) {
        drivePickerSelection.textContent = target
          ? `Selected: ${target.name || target.id}`
          : "";
      }
      if (drivePickerSelect) drivePickerSelect.disabled = !target;
    }

    // When nothing is explicitly highlighted, "Use this folder" picks the folder
    // we've drilled INTO (the current breadcrumb tail) — except the synthetic
    // My-Drive root, which is not a real selectable root folder id.
    function currentFolderAsSelection() {
      const tail = pickerTrail[pickerTrail.length - 1];
      if (!tail || tail.id === "root") return null;
      return tail;
    }

    function confirmPickerSelection() {
      const target = pickerSelected || currentFolderAsSelection();
      if (!target) return;
      selectResolvedFolder(target);
      setFact("folder-message", `Picked "${target.name || target.id}". Click "Save folder" to apply.`);
      closePicker();
    }

    // Fill the Root folder ID field and capture the folder's real name so the
    // save can include folder_name (the manual name field having been removed).
    function selectResolvedFolder(folder) {
      const id = String(folder.id || "");
      const name = String(folder.name || "");
      if (driveFolderIdInput) driveFolderIdInput.value = id;
      capturedFolderId = id;
      capturedFolderName = name;
      // Re-render the name-first display so the admin immediately sees the picked
      // folder's NAME (not the opaque id) before clicking Save.
      renderFolderDisplay();
    }

    // --- "+ New folder" -----------------------------------------------------
    function openNewFolderInput() {
      setNewFolderError("");
      if (drivePickerNewRow) drivePickerNewRow.hidden = false;
      if (drivePickerNewInput) {
        drivePickerNewInput.value = "";
        drivePickerNewInput.disabled = false;
        drivePickerNewInput.focus?.();
      }
    }

    function closeNewFolderInput() {
      if (drivePickerNewRow) drivePickerNewRow.hidden = true;
      if (drivePickerNewInput) drivePickerNewInput.value = "";
      setNewFolderError("");
      setNewFolderBusy(false);
    }

    async function createFolder() {
      const name = drivePickerNewInput?.value.trim() || "";
      if (!name) {
        setNewFolderError("Enter a folder name.");
        return;
      }
      const parent = currentParentId();
      setNewFolderError("");
      setNewFolderBusy(true);
      try {
        const response = await fetch("/api/admin/drive-folders", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ parent, name }),
        });
        // parseOkJson rejects with the server's {error} on a non-2xx (409 not
        // connected / 400 bad name / 429 / 502), surfaced inline below.
        const payload = await window.AuthExpired.parseOkJson(response, "Folder could not be created", reviewErrorFromPayload);
        const created = { id: String(payload.id || ""), name: String(payload.name || name) };
        if (!created.id) {
          setNewFolderError("Folder could not be created");
          return;
        }
        // Add the new folder to the current list and select it (fill the form).
        appendFolderToList(created);
        selectResolvedFolder(created);
        setFact("folder-message", `Created "${created.name}". Click "Save folder" to apply.`);
        closeNewFolderInput();
        closePicker();
      } catch (error) {
        setNewFolderError((error && error.message) || "Folder could not be created");
      } finally {
        setNewFolderBusy(false);
      }
    }

    function setNewFolderBusy(busy) {
      if (drivePickerNewInput) drivePickerNewInput.disabled = busy;
      if (drivePickerNewCreate) drivePickerNewCreate.disabled = busy;
    }

    function setNewFolderError(message) {
      if (!drivePickerNewError) return;
      const text = String(message || "");
      drivePickerNewError.textContent = text;
      drivePickerNewError.hidden = !text;
    }

    // Render one folder row into the existing list (reuses the same markup as
    // renderFolders) and highlight it as the current selection.
    function appendFolderToList(folder) {
      if (!drivePickerList) return;
      // A "No subfolders here" status would otherwise sit above the new row.
      setPickerStatus("");
      const id = String(folder.id || "");
      const name = String(folder.name || "");
      if (!id) return;
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "drive-picker-folder";
      const nameSpan = document.createElement("span");
      nameSpan.className = "drive-picker-folder-name";
      nameSpan.textContent = name || id;
      const openSpan = document.createElement("span");
      openSpan.className = "drive-picker-open";
      openSpan.textContent = "Open >";
      button.appendChild(nameSpan);
      button.appendChild(openSpan);
      button.addEventListener("click", (event) => {
        if (event.target === openSpan) {
          drillInto({ id, name });
        } else {
          selectFolder({ id, name }, button);
        }
      });
      openSpan.addEventListener("click", (event) => {
        event.stopPropagation();
        drillInto({ id, name });
      });
      li.appendChild(button);
      drivePickerList.appendChild(li);
      selectFolder({ id, name }, button);
    }

    function setPickerStatus(message) {
      if (!drivePickerStatus) return;
      const text = String(message || "");
      drivePickerStatus.textContent = text;
      drivePickerStatus.hidden = !text;
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
      // Seed the captured name from the persisted folder so a later save without
      // re-picking still carries the known name for this id.
      capturedFolderId = String(folder?.id || "");
      capturedFolderName = String(folder?.name || "");
      renderFolderDisplay();
    }

    // --- Name-first folder display -----------------------------------------
    // The raw id always lives in driveFolderIdInput (Save's single source of
    // truth). This decides which of the two faces the admin sees:
    //   * a known id  -> the read-only NAME display (id shown as muted secondary
    //     text); the friendly name is preferred, falling back to the id when the
    //     name isn't known (e.g. a stored id loaded with no name yet).
    //   * no id at all -> the raw-id editor row, so the admin can paste/browse.
    // If the display markup is absent the raw row is simply always shown.
    function renderFolderDisplay() {
      if (!driveFolderDisplay) return;
      const id = String(driveFolderIdInput?.value || "").trim();
      if (!id) {
        showFolderIdEditor();
        return;
      }
      const name = id === capturedFolderId ? String(capturedFolderName || "").trim() : "";
      if (driveFolderDisplayName) driveFolderDisplayName.textContent = name || id;
      if (driveFolderDisplayId) {
        // Show the id as secondary muted text. When the name is unknown the big
        // line already shows the id, so don't repeat it underneath.
        driveFolderDisplayId.textContent = name ? id : "";
        driveFolderDisplayId.hidden = !name;
      }
      driveFolderDisplay.hidden = false;
      if (driveFolderIdRow) driveFolderIdRow.hidden = true;
    }

    // "Edit ID": reveal the raw-id input so a power user can paste/edit an id by
    // hand. Hides the name display. Also the no-id fallback so the field is never
    // blank-and-stuck.
    function showFolderIdEditor() {
      if (driveFolderDisplay) driveFolderDisplay.hidden = true;
      if (driveFolderIdRow) driveFolderIdRow.hidden = false;
      driveFolderIdInput?.focus?.();
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

    return { load, openPicker, closePicker };
  }

  return { createController };
})();

function createAdminDriveController(options) {
  return AdminDriveView.createController(options);
}

// Node test-harness export (no-op in the browser): lets the FE unit test drive
// the real controller (and the folder-picker wiring) without a live DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminDriveView, createAdminDriveController };
}
