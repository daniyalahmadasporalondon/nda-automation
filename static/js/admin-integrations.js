const AdminIntegrationsView = (() => {
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

  const DEFAULT_QUERY_FALLBACK = [
    "in:inbox has:attachment (filename:docx OR filename:pdf) newer_than:30d -from:me",
    '(NDA OR MNDA OR "mutual NDA" OR "non-disclosure" OR "non disclosure"',
    'OR "non-disclosure agreement" OR "non disclosure agreement"',
    'OR "mutual non-disclosure" OR "mutual non disclosure"',
    'OR "confidentiality agreement" OR "mutual confidentiality agreement"',
    'OR confidentiality OR confidential OR "confidential disclosure agreement"',
    'OR CDA OR "confidentiality deed" OR "non-disclosure deed"',
    'OR "confidentiality undertaking" OR "letter of confidentiality"',
    'OR "data processing agreement" OR DPA)',
  ].join(" ");
  const DEFAULT_PARSED_FIELDS = "Subject headers, plain text body, HTML body, Gmail snippet, attachment filenames";
  const DEFAULT_SEARCH_TERMS = [
    "NDA",
    "MNDA",
    "mutual NDA",
    "non-disclosure",
    "non disclosure",
    "non-disclosure agreement",
    "non disclosure agreement",
    "mutual non-disclosure",
    "mutual non disclosure",
    "mutual non-disclosure agreement",
    "mutual non disclosure agreement",
    "mutual NDA agreement",
    "mutual MNDA",
    "confidentiality agreement",
    "mutual confidentiality agreement",
    "confidentiality",
    "confidential",
    "confidential disclosure agreement",
    "mutual confidential disclosure agreement",
    "CDA",
    "MCDA",
    "confidentiality deed",
    "non-disclosure deed",
    "mutual confidentiality deed",
    "mutual non-disclosure deed",
    "confidentiality undertaking",
    "non-disclosure undertaking",
    "letter of confidentiality",
    "confidentiality letter",
    "confidentiality terms",
    "confidentiality obligations",
    "confidential information",
    "confidential materials",
    "confidentiality provisions",
    "confidentiality clause",
    "confidentiality clauses",
    "secrecy agreement",
    "proprietary information agreement",
    "restricted disclosure",
    "do not disclose",
    "not disclose",
    "data processing agreement",
    "DPA",
  ];
  const DEFAULT_PARSED_TERMS = DEFAULT_SEARCH_TERMS.join(", ");
  const DEFAULT_FREQUENCY = "10_minutes";
  // Mirror the backend clamp (gmail_integration._MAX_GMAIL_IMPORT_LIMIT_CLAMP).
  // Keeping these in lockstep means the UI never offers (or posts) a value the
  // server would silently cap, so the saved value always matches what was typed.
  const MIN_IMPORT_LIMIT = 1;
  const MAX_IMPORT_LIMIT = 40;
  const DEFAULT_IMPORT_LIMIT = 20;
  const FREQUENCY_LABELS = {
    always_on: "Always on - every 1 minute",
    "10_minutes": "Every 10 minutes",
    "30_minutes": "Every 30 minutes",
    "1_hour": "Every 1 hour",
    "2_hours": "Every 2 hours",
  };

  function createController({
    state,
    gmailCard,
    gmailFacts,
    gmailOverall,
    gmailRecentSend,
    gmailRefreshButton,
    gmailSetupPanel,
    gmailToggle,
    gmailFrequencyControl,
    gmailSearchForm,
    gmailSearchTermsInput,
    gmailSearchSaveButton,
    gmailImportLimitForm,
    gmailImportLimitInput,
    gmailImportLimitSaveButton,
    gmailIntakeForm,
    gmailIntakeInput,
    gmailIntakeSaveButton,
    gmailSyncHistory,
    reviewErrorFromPayload,
  }) {
    gmailRefreshButton?.addEventListener("click", load);
    gmailToggle?.addEventListener("click", () => updateGmailToggle());
    gmailFrequencyControl?.querySelectorAll("[data-gmail-frequency]").forEach((button) => {
      button.addEventListener("click", () => updateGmailFrequency(button.dataset.gmailFrequency || DEFAULT_FREQUENCY));
    });
    gmailSearchForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      updateGmailSearchTerms();
    });
    // The import-limit save lives in a form so Enter submits; also wire the
    // button directly when it is rendered outside a <form>.
    gmailImportLimitForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      updateGmailImportLimit();
    });
    if (gmailImportLimitSaveButton && !gmailImportLimitForm) {
      gmailImportLimitSaveButton.addEventListener("click", (event) => {
        event.preventDefault();
        updateGmailImportLimit();
      });
    }
    gmailIntakeForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      updateGmailIntakePlaybook();
    });
    gmailSetupPanel?.addEventListener("click", (event) => {
      const disconnectButton = event.target.closest("[data-gmail-disconnect-role]");
      if (!disconnectButton) return;
      disconnectGmailRole(disconnectButton.dataset.gmailDisconnectRole || "all", disconnectButton);
    });

    async function load() {
      if (!gmailCard) return;
      setOverall("Checking", "pending");
      try {
        const [statusResponse, mattersResponse] = await Promise.all([
          fetch("/api/gmail/status"),
          fetch("/api/matters"),
        ]);
        const statusPayload = await statusResponse.json();
        const mattersPayload = await mattersResponse.json();
        if (!statusResponse.ok) throw reviewErrorFromPayload(statusPayload, "Gmail status could not load");
        if (!mattersResponse.ok) throw reviewErrorFromPayload(mattersPayload, "Repository could not load");
        renderGmail(statusPayload.gmail || {}, Array.isArray(mattersPayload.matters) ? mattersPayload.matters : []);
      } catch (error) {
        renderError(error.message || "Gmail status could not load");
      }
    }

    // Posts a Gmail settings change, checking response.ok BEFORE parsing so a
    // 401/500 (or a non-JSON proxy error page) surfaces the real status instead
    // of a generic failure or a raw JSON SyntaxError. Returns the parsed status
    // payload on success; throws a descriptive Error otherwise.
    async function postGmailSettings(body, fallbackMessage) {
      const response = await fetch("/api/gmail/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        // The error body may be JSON ({error}) or an HTML/blank proxy page;
        // never let a failed parse mask the underlying HTTP status.
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        throw reviewErrorFromPayload(
          payload,
          `${fallbackMessage} (HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""})`,
        );
      }
      // A 2xx with a malformed/blank body is still a save we cannot trust the
      // status from; treat it as an error rather than silently wiping state.
      try {
        return await response.json();
      } catch {
        throw new Error(`${fallbackMessage} (unreadable server response)`);
      }
    }

    async function updateGmailToggle() {
      const status = state.gmailStatus || {};
      // The toggle now PAUSES/RESUMES Gmail polling -- it no longer disconnects.
      // Disconnecting (removing the OAuth token) stays available via the per-role
      // controls in the connection setup panel and the account menu, so an
      // accidental tap on this switch can never drop the connection.
      //
      // For a signed-in user who has not connected Gmail yet there is no polling
      // to pause, so the switch still hands off to the Google consent screen to
      // create the connection in the first place.
      if (status.user_scoped === true) {
        const connected = isUserConnected(status.inbound) || isUserConnected(status.outbound);
        if (!connected) {
          // Hand off to the Google consent screen; the callback returns the page
          // connected, so the toggle comes back On.
          const connectUrl = status.connect_url || "/auth/gmail/start";
          window.location.href = withNext(connectUrl);
          return;
        }
      }
      // Connected (or env / shared-token mode): flip the polling switch. Off
      // pauses the scheduled poll, On resumes it -- the connection is untouched.
      const nextEnabled = status.settings?.sync_enabled === false;
      setToggleDisabled(true);
      setOverall("Saving", "pending");
      try {
        const payload = await postGmailSettings({ sync_enabled: nextEnabled }, "Gmail setting could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderToggleControls(state.gmailStatus || {});
      } finally {
        setToggleDisabled(false);
      }
    }

    async function updateGmailImportLimit() {
      const limit = parseImportLimit(gmailImportLimitInput?.value);
      if (limit === null) {
        setOverall("Add limit", "blocked");
        setFact("import-limit-copy", `Enter a whole number between ${MIN_IMPORT_LIMIT} and ${MAX_IMPORT_LIMIT}.`);
        return;
      }
      setImportLimitDisabled(true);
      setOverall("Saving", "pending");
      try {
        const payload = await postGmailSettings({ import_limit: limit }, "Gmail import limit could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
        // Honesty: the backend keeps a safety cap. If it reduced the requested
        // value, surface the warning inline so the effective (capped) value the
        // input now shows is explained rather than looking like a silent revert.
        if (payload.warning) {
          setOverall("Capped", "blocked");
          setFact("import-limit-copy", payload.warning);
        }
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        // Restore the input to the last-known-good value first, then surface the
        // error inline so the message is not immediately overwritten by the
        // re-render's "N messages per scheduled poll." copy.
        renderImportLimit(state.gmailStatus || {});
        setFact("import-limit-copy", error.message || "Gmail import limit could not save.");
      } finally {
        setImportLimitDisabled(false);
      }
    }

    async function updateGmailFrequency(syncFrequency) {
      const currentFrequency = state.gmailStatus?.settings?.sync_frequency || DEFAULT_FREQUENCY;
      if (syncFrequency === currentFrequency) return;
      setFrequencyDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sync_frequency: syncFrequency }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail sync frequency could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderFrequencyControl(state.gmailStatus?.settings?.sync_frequency || DEFAULT_FREQUENCY);
      } finally {
        setFrequencyDisabled(false);
      }
    }

    async function updateGmailSearchTerms() {
      const terms = parseSearchTerms(gmailSearchTermsInput?.value || "");
      if (!terms.length) {
        // Mirror the server's honest rejection: an empty field is NOT silently
        // reverted to the defaults; the admin must add a term for the save to take.
        setOverall("Add terms", "blocked");
        setFact("search-terms-copy", "Add at least one Gmail search term — it can't be empty.");
        return;
      }
      setSearchTermsDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ inbound_search_terms: terms }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail search terms could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        // Surface the server's 400 inline next to the field, then restore the
        // last-known-good terms, so the admin sees the save did NOT take.
        renderSearchTerms(state.gmailStatus || {});
        setFact("search-terms-copy", error.message || "Gmail search terms could not save.");
      } finally {
        setSearchTermsDisabled(false);
      }
    }

    async function updateGmailIntakePlaybook() {
      // Empty is a valid value (resets to the built-in default), so an empty
      // textarea is allowed -- unlike the search terms, which require at least one.
      const intakePlaybook = String(gmailIntakeInput?.value || "");
      if (intakePlaybook.length > 8000) {
        setOverall("Too long", "blocked");
        setFact("intake-copy", "NDA intake criteria must be under 8000 characters.");
        return;
      }
      setIntakeDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ intake_playbook: intakePlaybook }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "NDA intake criteria could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        // Surface the 400 inline next to the textarea rather than only in the
        // overall banner.
        setOverall(error.message || "Save failed", "blocked");
        setFact("intake-copy", error.message || "NDA intake criteria could not save.");
        renderIntakePlaybook(state.gmailStatus || {});
      } finally {
        setIntakeDisabled(false);
      }
    }

    async function disconnectGmailRole(role, control) {
      if (!role) return;
      if (control) {
        control.disabled = true;
        control.setAttribute("aria-busy", "true");
      }
      setOverall("Disconnecting", "pending");
      try {
        const response = await fetch("/api/gmail/disconnect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail disconnect failed");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Disconnect failed", "blocked");
        renderConnectionSetup(state.gmailStatus || {});
      } finally {
        if (control?.isConnected) {
          control.disabled = false;
          control.removeAttribute("aria-busy");
        }
      }
    }

    function renderGmail(status, matters) {
      state.gmailStatus = status;
      const inbound = status.inbound || {};
      const outbound = status.outbound || {};
      // Polling paused (sync_enabled === false) reads as Paused in the header,
      // as do the legacy per-role enabled flags.
      const pollingPaused = status.settings?.sync_enabled === false;
      const paused = pollingPaused || inbound.enabled === false || outbound.enabled === false;
      const ready = Boolean(inbound.ready && outbound.ready);
      setOverall(paused ? "Paused" : ready ? "Connected" : "Needs setup", paused ? "pending" : ready ? "ready" : "blocked");
      renderToggleControls(status);
      renderImportLimit(status);
      renderFrequencyControl(status.settings?.sync_frequency || DEFAULT_FREQUENCY);
      renderSearchTerms(status);
      renderIntakePlaybook(status);
      setFact("inbound-email", accountLabel(inbound));
      setFact("outbound-email", accountLabel(outbound));
      // Honesty: the scheduler checks the MASTER gate (sync_enabled) first, so
      // when polling is off the per-role inbound/outbound config is inert no
      // matter how it is configured. Say so rather than showing "Yes" for a row
      // that cannot run -- a non-blocking grey + label, the stored values are
      // left untouched.
      const inactiveSuffix = pollingPaused ? " — Gmail sync is off, inactive" : "";
      setFact("inbound-configured", (inbound.error || configuredLabel(inbound)) + inactiveSuffix);
      setFact("outbound-configured", (outbound.error || configuredLabel(outbound)) + inactiveSuffix);
      if (gmailCard) gmailCard.classList.toggle("gmail-sync-off", pollingPaused);
      setFact("inbound-token-source", tokenSourceLabel(inbound));
      setFact("outbound-token-source", tokenSourceLabel(outbound));
      setFact("default-query", inbound.query || DEFAULT_QUERY_FALLBACK);
      setFact("parsed-fields", parsingFieldsLabel(inbound.parsing));
      setFact("parsed-terms", parsingTermsLabel(inbound.parsing));
      setFact("last-sync", lastSyncLabel(status));
      renderConnectionSetup(status);
      renderSyncHistory(syncStatus(status).sync_history || []);
      renderRecentSend(matters);
    }

    function renderConnectionSetup(status) {
      if (!gmailSetupPanel) return;
      const roles = [
        { account: status.inbound || {}, id: "inbound", title: "Inbound connection" },
        { account: status.outbound || {}, id: "outbound", title: "Outbound connection" },
      ];
      // The Gmail toggle above is the single connect/disconnect control, so the
      // setup panel shows the per-role rows as read-only status only.
      const rows = roles.map((role) => renderConnectionRow(role, status)).join("");
      gmailSetupPanel.innerHTML = rows;
    }

    function renderConnectionRow(role, status) {
      const account = role.account || {};
      const token = account.token || {};
      const paused = account.enabled === false;
      const ready = account.ready === true;
      const tone = ready ? "ready" : paused ? "pending" : "blocked";
      const setupState = gmailConnectionState(role.id, account, token, status);
      const statusLabel = paused ? "Paused" : ready ? "Ready" : setupState.statusLabel;
      const scopeCopy = gmailScopeLabel(account, token);
      return `
        <div class="integration-connection-row ${tone}">
          <div class="integration-connection-top">
            <strong>${html(role.title)}</strong>
            <span>${html(statusLabel)}</span>
          </div>
          <dl>
            <div>
              <dt>Account</dt>
              <dd>${html(account.email || "Not resolved")}</dd>
            </div>
            <div>
              <dt>Token</dt>
              <dd>${html(tokenSourceLabel(account))}</dd>
            </div>
            <div>
              <dt>Next step</dt>
              <dd>${html(setupState.nextStep)}</dd>
            </div>
            ${scopeCopy ? `
              <div>
                <dt>Scope</dt>
                <dd>${html(scopeCopy)}</dd>
              </div>
            ` : ""}
          </dl>
        </div>
      `;
    }

    function renderSyncHistory(syncHistory) {
      if (!gmailSyncHistory) return;
      const runs = Array.isArray(syncHistory) ? syncHistory.slice(0, 5) : [];
      if (!runs.length) {
        gmailSyncHistory.innerHTML = '<div class="integration-sync-history-empty">No sync runs recorded</div>';
        return;
      }
      gmailSyncHistory.innerHTML = runs.map((run) => {
        const imported = Number(run.imported_count || 0);
        const skipped = Number(run.skipped_count || 0);
        const duplicate = Number(run.duplicate_count || 0);
        const deduplicated = Number(run.deduplicated_count || 0);
        const reviewFailed = Number(run.review_failed_count || 0);
        const status = run.status === "error" ? "Error" : "Complete";
        const query = run.query ? `<p class="integration-sync-history-query">${html(run.query)}</p>` : "";
        const error = run.error ? `<p class="integration-sync-history-error">${html(run.error)}</p>` : "";
        return `
          <article class="integration-sync-history-item ${run.status === "error" ? "error" : ""}">
            <div class="integration-sync-history-top">
              <strong>${html(formatDateTime(run.finished_at || run.started_at) || run.finished_at || run.started_at || "-")}</strong>
              <span>${html(status)}</span>
            </div>
            <p class="integration-sync-history-counts">${imported} imported / ${skipped} skipped / ${duplicate} duplicates / ${deduplicated} stale duplicates removed / ${reviewFailed} review failures</p>
            ${query}
            ${error}
          </article>
        `;
      }).join("");
    }

    function renderRecentSend(matters) {
      if (!gmailRecentSend) return;
      const outboundEmail = String(state.gmailStatus?.outbound?.email || "").toLowerCase();
      const recent = matters
        .filter((matter) => {
          if (!matter?.last_outbound_at) return false;
          if (!outboundEmail) return true;
          return String(matter.last_outbound_account || "").toLowerCase() === outboundEmail;
        })
        .sort((left, right) => String(right.last_outbound_at).localeCompare(String(left.last_outbound_at)))[0];
      if (!recent) {
        gmailRecentSend.innerHTML = `
          <div>
            <dt>Status</dt>
            <dd>No outbound sends recorded</dd>
          </div>
        `;
        return;
      }
      gmailRecentSend.innerHTML = `
        <div>
          <dt>From</dt>
          <dd>${html(recent.last_outbound_account || "-")}</dd>
        </div>
        <div>
          <dt>To</dt>
          <dd>${html(recent.last_outbound_to || "-")}</dd>
        </div>
        <div>
          <dt>Subject</dt>
          <dd>${html(recent.last_outbound_subject || recent.subject || "-")}</dd>
        </div>
        <div>
          <dt>Sent</dt>
          <dd>${html(formatDateTime(recent.last_outbound_at) || "-")}</dd>
        </div>
      `;
    }

    function renderError(message) {
      setOverall("Unavailable", "blocked");
      setFact("inbound-email", message);
      setFact("outbound-email", message);
      setFact("inbound-configured", "Unknown");
      setFact("outbound-configured", "Unknown");
      setFact("inbound-token-source", "Unknown");
      setFact("outbound-token-source", "Unknown");
      setFact("default-query", DEFAULT_QUERY_FALLBACK);
      setFact("parsed-fields", DEFAULT_PARSED_FIELDS);
      setFact("parsed-terms", DEFAULT_PARSED_TERMS);
      setFact("last-sync", lastSyncLabel(state.gmailStatus || {}));
      renderConnectionSetup(state.gmailStatus || {});
      renderSyncHistory(syncStatus(state.gmailStatus || {}).sync_history || []);
      renderToggleControls(state.gmailStatus || {});
      renderImportLimit(state.gmailStatus || {});
      renderFrequencyControl(state.gmailStatus?.settings?.sync_frequency || DEFAULT_FREQUENCY);
      renderSearchTerms(state.gmailStatus || {});
    }

    function setOverall(label, tone) {
      if (!gmailOverall) return;
      gmailOverall.textContent = label;
      gmailOverall.classList.toggle("ready", tone === "ready");
      gmailOverall.classList.toggle("blocked", tone === "blocked");
      gmailOverall.classList.toggle("pending", tone === "pending");
    }

    function setFact(key, value) {
      const node = gmailCard?.querySelector(`[data-admin-gmail="${key}"]`) || gmailFacts?.querySelector(`[data-admin-gmail="${key}"]`);
      if (node) node.textContent = value;
    }

    function renderToggleControls(status) {
      const inbound = status.inbound || {};
      const outbound = status.outbound || {};
      // The toggle now reflects whether POLLING is on. A signed-in user who has
      // not connected Gmail yet has no polling to show, so the switch reads Off
      // (its tap starts the connect flow); once connected -- and in env/shared-
      // token mode -- the switch follows settings.sync_enabled.
      const needsConnect = status.user_scoped === true
        && !(isUserConnected(inbound) || isUserConnected(outbound));
      const pollingOn = status.settings?.sync_enabled !== false;
      const ready = inbound.ready === true && outbound.ready === true;
      const on = needsConnect ? false : pollingOn;
      renderToggle(gmailToggle, on);
      renderToggleIntent(status, { on, needsConnect, ready });
      setFact("enabled-copy", needsConnect ? "Not connected" : on ? "Polling on" : "Polling off");
    }

    function renderToggle(button, enabled) {
      if (!button) return;
      button.setAttribute("aria-checked", enabled ? "true" : "false");
      button.classList.toggle("on", enabled);
      button.classList.toggle("off", !enabled);
    }

    function renderToggleIntent(status, { on, needsConnect, ready }) {
      if (!gmailToggle) return;
      // A not-yet-connected user gets the connect affordance. Otherwise the
      // switch pauses or resumes polling (the connection is never touched here);
      // when polling is nominally on but the mailboxes are not wired up yet, say
      // so rather than offering to "pause" a sync that cannot run.
      let label;
      if (needsConnect) {
        label = gmailToggleLabel(status);
      } else if (on && !ready) {
        label = "Gmail enabled; setup required";
      } else if (on) {
        label = "Pause Gmail polling";
      } else {
        label = "Resume Gmail polling";
      }
      gmailToggle.setAttribute("aria-label", label);
      gmailToggle.title = label;
    }

    function setToggleDisabled(disabled) {
      if (gmailToggle) gmailToggle.disabled = disabled;
    }

    function renderImportLimit(status) {
      const limit = importLimitFromStatus(status);
      if (gmailImportLimitInput) gmailImportLimitInput.value = String(limit);
      setFact("import-limit-copy", `${limit} messages per scheduled poll.`);
    }

    function setImportLimitDisabled(disabled) {
      if (gmailImportLimitInput) gmailImportLimitInput.disabled = disabled;
      if (gmailImportLimitSaveButton) gmailImportLimitSaveButton.disabled = disabled;
    }

    function renderFrequencyControl(syncFrequency) {
      const frequency = FREQUENCY_LABELS[syncFrequency] ? syncFrequency : DEFAULT_FREQUENCY;
      gmailFrequencyControl?.querySelectorAll("[data-gmail-frequency]").forEach((button) => {
        const selected = button.dataset.gmailFrequency === frequency;
        button.setAttribute("aria-pressed", selected ? "true" : "false");
        button.classList.toggle("active", selected);
      });
      setFact("sync-frequency-copy", `${FREQUENCY_LABELS[frequency]}.`);
    }

    function setFrequencyDisabled(disabled) {
      gmailFrequencyControl?.querySelectorAll("[data-gmail-frequency]").forEach((button) => {
        button.disabled = disabled;
      });
    }

    function renderSearchTerms(status) {
      const terms = searchTermsFromStatus(status);
      if (gmailSearchTermsInput) gmailSearchTermsInput.value = terms.join("\n");
      setFact("search-terms-copy", `${terms.length} Gmail search terms.`);
    }

    function setSearchTermsDisabled(disabled) {
      if (gmailSearchTermsInput) gmailSearchTermsInput.disabled = disabled;
      if (gmailSearchSaveButton) gmailSearchSaveButton.disabled = disabled;
    }

    function renderIntakePlaybook(status) {
      if (!gmailIntakeInput) return;
      // The effective playbook: the stored criteria when set, else the built-in
      // default surfaced by the server status payload (empty stays empty so the
      // operator can see they are on the default and the placeholder hint shows).
      const stored = String(status?.settings?.intake_playbook || "");
      const effective = stored || String(status?.intake_playbook_default || "");
      gmailIntakeInput.value = stored;
      if (effective) gmailIntakeInput.setAttribute("placeholder", effective);
    }

    function setIntakeDisabled(disabled) {
      if (gmailIntakeInput) gmailIntakeInput.disabled = disabled;
      if (gmailIntakeSaveButton) gmailIntakeSaveButton.disabled = disabled;
    }

    return { load, renderGmailStatus: (gmailStatus) => renderGmail(gmailStatus || {}, []) };
  }

  function accountLabel(account) {
    if (account?.email) return account.email;
    return account?.error || account?.email || "Not connected";
  }

  function configuredLabel(account) {
    if (account?.configured === true) return "Yes";
    if (account?.configured === false) return "No";
    return account?.ready ? "Yes" : "Unknown";
  }

  function tokenSourceLabel(account) {
    const token = account?.token || {};
    const label = String(token.label || "").trim();
    if (token.source === "environment") return `Environment: ${label || "configured env var"}`;
    if (token.source === "local_data") return `Local data: ${label || "data/gmail token"}`;
    if (token.source === "user_data") return `User Gmail: ${label || "connected OAuth token"}`;
    if (token.source === "missing") return `Missing: ${label || "token path"}`;
    if (token.configured === true) return "Configured";
    if (token.configured === false) return "Missing";
    return "Unknown";
  }

  function parsingFieldsLabel(parsing) {
    const fields = Array.isArray(parsing?.fields) ? parsing.fields.filter(Boolean) : [];
    if (!fields.length) return DEFAULT_PARSED_FIELDS;
    const mode = String(parsing?.mode || "").trim();
    return mode ? `${fields.join(", ")}. ${mode}` : fields.join(", ");
  }

  function parsingTermsLabel(parsing) {
    const terms = Array.isArray(parsing?.terms) ? parsing.terms.filter(Boolean) : [];
    return terms.length ? terms.join(", ") : DEFAULT_PARSED_TERMS;
  }

  function searchTermsFromStatus(status) {
    const settingsTerms = Array.isArray(status?.settings?.inbound_search_terms) ? status.settings.inbound_search_terms : [];
    const parsingTerms = Array.isArray(status?.inbound?.parsing?.terms) ? status.inbound.parsing.terms : [];
    const terms = settingsTerms.length ? settingsTerms : parsingTerms;
    return terms.length ? terms.map((term) => String(term)).filter(Boolean) : DEFAULT_SEARCH_TERMS;
  }

  function parseSearchTerms(value) {
    const terms = [];
    const seen = new Set();
    String(value || "")
      .split(/\n|,/)
      .map((term) => term.replace(/^["'()]+|["'()]+$/g, "").trim().replace(/\s+/g, " "))
      .filter(Boolean)
      .forEach((term) => {
        const key = term.toLowerCase();
        if (seen.has(key)) return;
        terms.push(term);
        seen.add(key);
      });
    return terms.slice(0, 60);
  }

  function importLimitFromStatus(status) {
    const raw = status?.settings?.import_limit;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < MIN_IMPORT_LIMIT) return DEFAULT_IMPORT_LIMIT;
    // Clamp the displayed value into the supported band so a stale or
    // over-the-cap stored value never renders something the input cannot hold.
    return Math.min(Math.floor(value), MAX_IMPORT_LIMIT);
  }

  function parseImportLimit(value) {
    // Reject blanks, decimals, and non-numeric input; clamp the top end to the
    // backend cap so the POST never carries a value the server would reduce.
    const trimmed = String(value == null ? "" : value).trim();
    if (!/^\d+$/.test(trimmed)) return null;
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed) || parsed < MIN_IMPORT_LIMIT) return null;
    return Math.min(parsed, MAX_IMPORT_LIMIT);
  }

  function gmailConnectionState(role, account, token, status) {
    const setup = status?.setup || {};
    const recovery = account?.recovery || {};
    const state = recovery.state || setup.state || "";
    const message = String(recovery.message || setup.message || "").trim();
    if (account?.enabled === false) {
      return {
        nextStep: `Turn on ${role} email when this mailbox should be active.`,
        statusLabel: "Paused",
      };
    }
    if (account?.ready === true) {
      return {
        nextStep: role === "inbound" ? "Ready for scheduled sync." : "Ready to send redlines.",
        statusLabel: "Ready",
      };
    }
    // The backend sets a specific readiness block reason when a connected token
    // fetches its profile fine but still can't actually import (missing scope, or
    // an expired/un-refreshable token). Surface that exact reason instead of a
    // generic "needs setup" so a broken token reads RED with a real explanation.
    const blockReason = String(account?.reason || "").trim();
    if (blockReason) {
      const missingScope = /missing permission/i.test(blockReason);
      return {
        nextStep: blockReason,
        statusLabel: missingScope ? "Needs Gmail scope" : "Reconnect Gmail",
      };
    }
    if (
      state === "missing_oauth_config"
      || status?.google_oauth_configured === false
      || status?.oauth_configured === false
      || setup.google_oauth_configured === false
    ) {
      return {
        nextStep: message || "Configure the Google OAuth client ID and secret, then refresh this page.",
        statusLabel: "Needs OAuth config",
      };
    }
    if (
      state === "sign_in_required"
      || (status?.user_scoped === true && status?.signed_in === false)
    ) {
      return {
        nextStep: message || "Sign in with Google, then use the Gmail switch to connect this mailbox.",
        statusLabel: "Needs Google sign-in",
      };
    }
    if (state === "missing_token") {
      return {
        nextStep: message || `Use the Gmail switch above to create this user's ${role} Gmail token.`,
        statusLabel: "Needs token",
      };
    }
    if (state === "missing_scope" || gmailScopeLabel(account, token)) {
      return {
        nextStep: message || `Reconnect Gmail and approve the required ${role} scope.`,
        statusLabel: "Needs Gmail scope",
      };
    }
    if (account?.connect_url || token?.label === `Connect Gmail for ${role}`) {
      return {
        nextStep: `Use the Gmail switch above to connect this user's ${role} Gmail account.`,
        statusLabel: "Needs connection",
      };
    }
    if (token?.source === "environment" && token?.configured === false) {
      return {
        nextStep: `Fix ${role === "inbound" ? "NDA_GMAIL_INBOUND_TOKEN_PATH" : "NDA_GMAIL_OUTBOUND_TOKEN_PATH"} or unset it to use data/gmail/${role}-token.json.`,
        statusLabel: "Needs setup",
      };
    }
    if (token?.configured === false) {
      return {
        nextStep: `Add data/gmail/${role}-token.json or set ${role === "inbound" ? "NDA_GMAIL_INBOUND_TOKEN_PATH" : "NDA_GMAIL_OUTBOUND_TOKEN_PATH"}.`,
        statusLabel: "Needs setup",
      };
    }
    return {
      nextStep: account?.error || "Reconnect Gmail and refresh status.",
      statusLabel: "Needs setup",
    };
  }

  function gmailToggleLabel(status = {}) {
    const setup = status.setup || {};
    if (
      setup.state === "missing_oauth_config"
      || status.google_oauth_configured === false
      || status.oauth_configured === false
      || setup.google_oauth_configured === false
    ) {
      return "Google OAuth is not configured";
    }
    if (
      setup.state === "sign_in_required"
      || (status.user_scoped === true && status.signed_in === false)
    ) return "Sign in with Google to connect Gmail";
    return "Connect Gmail";
  }

  function gmailScopeLabel(account, token) {
    const scopes = [];
    if (Array.isArray(account?.missing_scopes)) scopes.push(...account.missing_scopes);
    if (Array.isArray(token?.missing_scopes)) scopes.push(...token.missing_scopes);
    if (Array.isArray(token?.scope_status?.missing)) scopes.push(...token.scope_status.missing);
    if (Array.isArray(account?.recovery?.scope_status?.missing)) scopes.push(...account.recovery.scope_status.missing);
    if (account?.scope_status === "missing" || token?.scope_status === "missing") scopes.push("required Gmail scope");
    if (token?.scope_status?.ok === false || account?.recovery?.scope_status?.ok === false) scopes.push("required Gmail scope");
    const unique = [...new Set(scopes.map((scope) => String(scope).trim()).filter(Boolean))];
    return unique.length ? `Missing: ${unique.join(", ")}` : "";
  }

  function isUserConnected(account) {
    return account?.token?.source === "user_data" && account?.token?.configured === true;
  }

  function withNext(url) {
    const target = new URL(url, window.location.origin);
    if (!target.searchParams.has("next")) {
      target.searchParams.set("next", window.location.pathname + window.location.search);
    }
    return `${target.pathname}${target.search}${target.hash}`;
  }

  function syncStatus(status) {
    return status?.sync || status?.settings || {};
  }

  function lastSyncLabel(statusOrSettings) {
    const settings = syncStatus(statusOrSettings);
    const inbound = statusOrSettings?.inbound || {};
    if (inbound.enabled === false) return "Gmail inbound paused";
    if (inbound.ready === false) return `Gmail inbound setup required: ${inbound.error || "check inbound setup"}`;
    if (!settings?.last_sync_at) return "Waiting for scheduled sync";
    const parts = [formatDateTime(settings.last_sync_at) || settings.last_sync_at];
    const imported = Number(settings.last_sync_imported_count || 0);
    const skipped = Number(settings.last_sync_skipped_count || 0);
    parts.push(`${imported} imported / ${skipped} skipped`);
    return parts.join(" - ");
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString(undefined, {
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
    });
  }

  return { createController, importLimitFromStatus, parseImportLimit, MAX_IMPORT_LIMIT, MIN_IMPORT_LIMIT };
})();

function createAdminIntegrationsController(options) {
  return AdminIntegrationsView.createController(options);
}

// Node test-harness export (no-op in the browser): lets the FE unit test drive
// the controller and exercise the pure import-limit helpers without a real DOM.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminIntegrationsView, createAdminIntegrationsController };
}
