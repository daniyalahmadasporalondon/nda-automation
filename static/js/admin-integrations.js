const AdminIntegrationsView = (() => {
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

    async function updateGmailToggle() {
      // Inbound + outbound are one Gmail system, so the single toggle flips both
      // role flags together (the backend still gates each role separately under
      // the hood). On only when both are on: a tap from on -> off disables both;
      // from off or a mixed state -> on enables both.
      const inboundOn = state.gmailStatus?.inbound?.enabled !== false;
      const outboundOn = state.gmailStatus?.outbound?.enabled !== false;
      const nextEnabled = !(inboundOn && outboundOn);
      setToggleDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ inbound_enabled: nextEnabled, outbound_enabled: nextEnabled }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail setting could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderToggleControls(state.gmailStatus || {});
      } finally {
        setToggleDisabled(false);
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
        setOverall("Add terms", "blocked");
        setFact("search-terms-copy", "Add at least one search term.");
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
        renderSearchTerms(state.gmailStatus || {});
      } finally {
        setSearchTermsDisabled(false);
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
      const paused = inbound.enabled === false || outbound.enabled === false;
      const ready = Boolean(inbound.ready && outbound.ready);
      setOverall(paused ? "Paused" : ready ? "Connected" : "Needs setup", paused ? "pending" : ready ? "ready" : "blocked");
      renderToggleControls(status);
      renderFrequencyControl(status.settings?.sync_frequency || DEFAULT_FREQUENCY);
      renderSearchTerms(status);
      setFact("inbound-email", accountLabel(inbound));
      setFact("outbound-email", accountLabel(outbound));
      setFact("inbound-configured", inbound.error || configuredLabel(inbound));
      setFact("outbound-configured", outbound.error || configuredLabel(outbound));
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
      const rows = roles.map((role) => renderConnectionRow(role)).join("");
      gmailSetupPanel.innerHTML = rows + renderUnifiedGmailActions(status);
    }

    function renderConnectionRow(role) {
      const account = role.account || {};
      const token = account.token || {};
      const paused = account.enabled === false;
      const ready = account.ready === true;
      const tone = ready ? "ready" : paused ? "pending" : "blocked";
      const statusLabel = paused ? "Paused" : ready ? "Ready" : "Needs setup";
      return `
        <div class="integration-connection-row ${tone}">
          <div class="integration-connection-top">
            <strong>${escapeHtml(role.title)}</strong>
            <span>${escapeHtml(statusLabel)}</span>
          </div>
          <dl>
            <div>
              <dt>Account</dt>
              <dd>${escapeHtml(account.email || "Not resolved")}</dd>
            </div>
            <div>
              <dt>Token</dt>
              <dd>${escapeHtml(tokenSourceLabel(account))}</dd>
            </div>
            <div>
              <dt>Next step</dt>
              <dd>${escapeHtml(connectionNextStep(role.id, account, token))}</dd>
            </div>
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
        const query = run.query ? `<p class="integration-sync-history-query">${escapeHtml(run.query)}</p>` : "";
        const error = run.error ? `<p class="integration-sync-history-error">${escapeHtml(run.error)}</p>` : "";
        return `
          <article class="integration-sync-history-item ${run.status === "error" ? "error" : ""}">
            <div class="integration-sync-history-top">
              <strong>${escapeHtml(formatDateTime(run.finished_at || run.started_at) || run.finished_at || run.started_at || "-")}</strong>
              <span>${escapeHtml(status)}</span>
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
          <dd>${escapeHtml(recent.last_outbound_account || "-")}</dd>
        </div>
        <div>
          <dt>To</dt>
          <dd>${escapeHtml(recent.last_outbound_to || "-")}</dd>
        </div>
        <div>
          <dt>Subject</dt>
          <dd>${escapeHtml(recent.last_outbound_subject || recent.subject || "-")}</dd>
        </div>
        <div>
          <dt>Sent</dt>
          <dd>${escapeHtml(formatDateTime(recent.last_outbound_at) || "-")}</dd>
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
      const enabled = inbound.enabled !== false && outbound.enabled !== false;
      renderToggle(gmailToggle, enabled);
      setFact("enabled-copy", enabled ? "On" : "Off");
    }

    function renderToggle(button, enabled) {
      if (!button) return;
      button.setAttribute("aria-checked", enabled ? "true" : "false");
      button.classList.toggle("on", enabled);
      button.classList.toggle("off", !enabled);
    }

    function setToggleDisabled(disabled) {
      if (gmailToggle) gmailToggle.disabled = disabled;
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

  function connectionNextStep(role, account, token) {
    if (account?.enabled === false) return `Turn on ${role} email when this mailbox should be active.`;
    if (account?.ready === true) return role === "inbound" ? "Ready for scheduled sync." : "Ready to send redlines.";
    if (account?.connect_url || token?.label === `Connect Gmail for ${role}`) {
      return `Connect this user's ${role} Gmail account.`;
    }
    if (token?.source === "environment" && token?.configured === false) {
      return `Fix ${role === "inbound" ? "NDA_GMAIL_INBOUND_TOKEN_PATH" : "NDA_GMAIL_OUTBOUND_TOKEN_PATH"} or unset it to use data/gmail/${role}-token.json.`;
    }
    if (token?.configured === false) {
      return `Add data/gmail/${role}-token.json or set ${role === "inbound" ? "NDA_GMAIL_INBOUND_TOKEN_PATH" : "NDA_GMAIL_OUTBOUND_TOKEN_PATH"}.`;
    }
    return account?.error || "Reconnect Gmail and refresh status.";
  }

  function isUserConnected(account) {
    return account?.token?.source === "user_data" && account?.token?.configured === true;
  }

  // Inbound and outbound share a single Gmail login: one OAuth consent grants
  // gmail.readonly + gmail.send + gmail.metadata and the backend saves both role
  // tokens from that single grant (role="all"). So the Admin panel exposes ONE
  // Connect/Disconnect Gmail action, not a per-role button each -- the per-role
  // rows above stay as read-only status so the single login's coverage is visible.
  function renderUnifiedGmailActions(status) {
    if (!status || status.user_scoped !== true) return "";
    const inbound = status.inbound || {};
    const outbound = status.outbound || {};
    const connected = isUserConnected(inbound) || isUserConnected(outbound);
    const needsConnect = inbound.ready !== true || outbound.ready !== true;
    const connectUrl = status.connect_url || "/auth/gmail/start";
    const actions = [];
    if (connectUrl && needsConnect) {
      const label = connected ? "Reconnect Gmail" : "Connect Gmail";
      actions.push(`<a class="integration-connection-action" href="${escapeHtml(withNext(connectUrl))}">${escapeHtml(label)}</a>`);
    }
    if (connected) {
      actions.push('<button class="integration-connection-action secondary" type="button" data-gmail-disconnect-role="all">Disconnect Gmail</button>');
    }
    return actions.length ? `<div class="integration-connection-actions integration-connection-actions-unified">${actions.join("")}</div>` : "";
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

  return { createController };
})();

function createAdminIntegrationsController(options) {
  return AdminIntegrationsView.createController(options);
}
