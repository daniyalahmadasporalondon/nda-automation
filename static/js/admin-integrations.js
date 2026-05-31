const AdminIntegrationsView = (() => {
  const DEFAULT_QUERY_FALLBACK = [
    "has:attachment (filename:docx OR filename:pdf) newer_than:30d",
    '(subject:NDA OR subject:"non-disclosure" OR subject:"non disclosure"',
    'OR subject:"non-disclosure agreement" OR subject:"non disclosure agreement"',
    'OR subject:"confidentiality agreement" OR subject:confidentiality OR subject:confidential)',
  ].join(" ");
  const CADENCE_LABELS = {
    manual: "Manual sync only",
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
    gmailSyncButton,
    gmailInboundToggle,
    gmailOutboundToggle,
    gmailCadenceControl,
    reviewErrorFromPayload,
    syncGmail,
  }) {
    gmailRefreshButton?.addEventListener("click", load);
    gmailSyncButton?.addEventListener("click", sync);
    gmailInboundToggle?.addEventListener("click", () => updateGmailToggle("inbound"));
    gmailOutboundToggle?.addEventListener("click", () => updateGmailToggle("outbound"));
    gmailCadenceControl?.querySelectorAll("[data-gmail-cadence]").forEach((button) => {
      button.addEventListener("click", () => updateGmailCadence(button.dataset.gmailCadence || "manual"));
    });

    async function sync() {
      if (!syncGmail) return;
      if (state.gmailStatus?.inbound?.enabled === false) {
        setOverall("Inbound off", "blocked");
        return;
      }
      setOverall("Syncing", "pending");
      const result = await syncGmail({ button: gmailSyncButton });
      if (result?.error) {
        setOverall("Sync failed", "blocked");
      }
      await load();
    }

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

    async function updateGmailToggle(role) {
      const current = state.gmailStatus?.[role] || {};
      const nextEnabled = current.enabled === false;
      const payloadKey = `${role}_enabled`;
      setToggleDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [payloadKey]: nextEnabled }),
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

    async function updateGmailCadence(syncCadence) {
      const currentCadence = state.gmailStatus?.settings?.sync_cadence || "manual";
      if (syncCadence === currentCadence) return;
      setCadenceDisabled(true);
      setOverall("Saving", "pending");
      try {
        const response = await fetch("/api/gmail/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sync_cadence: syncCadence }),
        });
        const payload = await response.json();
        if (!response.ok) throw reviewErrorFromPayload(payload, "Gmail sync cadence could not save");
        state.gmailStatus = payload.gmail || state.gmailStatus || {};
        await load();
      } catch (error) {
        setOverall(error.message || "Save failed", "blocked");
        renderCadenceControl(state.gmailStatus?.settings?.sync_cadence || "manual");
      } finally {
        setCadenceDisabled(false);
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
      renderCadenceControl(status.settings?.sync_cadence || "manual");
      setFact("inbound-email", accountLabel(inbound));
      setFact("outbound-email", accountLabel(outbound));
      setFact("inbound-configured", configuredLabel(inbound));
      setFact("outbound-configured", configuredLabel(outbound));
      setFact("default-query", inbound.query || DEFAULT_QUERY_FALLBACK);
      setFact("last-sync", lastSyncLabel(state.gmailLastSync));
      renderRecentSend(matters);
    }

    function renderRecentSend(matters) {
      if (!gmailRecentSend) return;
      const recent = matters
        .filter((matter) => matter && matter.last_outbound_at)
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
      setFact("default-query", DEFAULT_QUERY_FALLBACK);
      setFact("last-sync", lastSyncLabel(state.gmailLastSync));
      renderToggleControls(state.gmailStatus || {});
      renderCadenceControl(state.gmailStatus?.settings?.sync_cadence || "manual");
    }

    function setLastSync(sync) {
      state.gmailLastSync = sync;
      setFact("last-sync", lastSyncLabel(sync));
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
      renderToggle(gmailInboundToggle, inbound.enabled !== false);
      renderToggle(gmailOutboundToggle, outbound.enabled !== false);
      setFact("inbound-enabled-copy", inbound.enabled === false ? "Off" : "On");
      setFact("outbound-enabled-copy", outbound.enabled === false ? "Off" : "On");
      if (gmailSyncButton) {
        gmailSyncButton.disabled = inbound.enabled === false;
      }
    }

    function renderToggle(button, enabled) {
      if (!button) return;
      button.setAttribute("aria-checked", enabled ? "true" : "false");
      button.classList.toggle("on", enabled);
      button.classList.toggle("off", !enabled);
    }

    function setToggleDisabled(disabled) {
      [gmailInboundToggle, gmailOutboundToggle].forEach((button) => {
        if (button) button.disabled = disabled;
      });
    }

    function renderCadenceControl(syncCadence) {
      const cadence = CADENCE_LABELS[syncCadence] ? syncCadence : "manual";
      gmailCadenceControl?.querySelectorAll("[data-gmail-cadence]").forEach((button) => {
        const selected = button.dataset.gmailCadence === cadence;
        button.setAttribute("aria-pressed", selected ? "true" : "false");
        button.classList.toggle("active", selected);
      });
      setFact("sync-cadence-copy", `${CADENCE_LABELS[cadence]}.`);
    }

    function setCadenceDisabled(disabled) {
      gmailCadenceControl?.querySelectorAll("[data-gmail-cadence]").forEach((button) => {
        button.disabled = disabled;
      });
    }

    return { load, setLastSync };
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

  function lastSyncLabel(sync) {
    if (!sync?.synced_at) return "Not run in this session";
    const parts = [formatDateTime(sync.synced_at) || sync.synced_at];
    if (sync.account) parts.push(sync.account);
    const imported = Number(sync.imported_count || 0);
    const skipped = Number(sync.skipped_count || 0);
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
