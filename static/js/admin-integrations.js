const AdminIntegrationsView = (() => {
  const DEFAULT_QUERY_FALLBACK = "has:attachment (filename:docx OR filename:pdf) newer_than:30d";

  function createController({
    state,
    gmailCard,
    gmailFacts,
    gmailOverall,
    gmailRecentSend,
    gmailRefreshButton,
    reviewErrorFromPayload,
  }) {
    gmailRefreshButton?.addEventListener("click", load);

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

    function renderGmail(status, matters) {
      const inbound = status.inbound || {};
      const outbound = status.outbound || {};
      const ready = Boolean(inbound.ready && outbound.ready);
      setOverall(ready ? "Connected" : "Needs setup", ready ? "ready" : "blocked");
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
      const node = gmailFacts?.querySelector(`[data-admin-gmail="${key}"]`);
      if (node) node.textContent = value;
    }

    return { load, setLastSync };
  }

  function accountLabel(account) {
    if (account?.ready && account.email) return account.email;
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
