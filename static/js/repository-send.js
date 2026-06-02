const RepositorySend = (() => {
  function renderSendComposer({ confirmingSend, gmailStatus, matter, recipient }) {
    if (!confirmingSend) return "";
    const subject = defaultOutboundSubject(matter);
    const body = defaultOutboundBody(matter);
    return `
        <section class="repository-send-composer" aria-label="Outbound redline email">
          <dl class="repository-send-route">
            <div>
              <dt>From</dt>
              <dd>${escapeHtml(outboundAccountLabel(gmailStatus))}</dd>
            </div>
            <div>
              <dt>To</dt>
              <dd>${escapeHtml(recipient)}</dd>
            </div>
          </dl>
          <label class="repository-send-field" for="repositorySendSubject">
            <span>Subject</span>
            <input id="repositorySendSubject" type="text" value="${escapeHtml(subject)}" autocomplete="off">
          </label>
          <label class="repository-send-field" for="repositorySendBody">
            <span>Message</span>
            <textarea id="repositorySendBody" rows="7">${escapeHtml(body)}</textarea>
          </label>
        </section>
      `;
  }

  function outboundAccountLabel(gmailStatus) {
    const outbound = gmailStatus?.outbound || {};
    if (outbound.ready && outbound.email) return outbound.email;
    return outbound.error || outbound.email || "Outbound Gmail not connected";
  }

  function defaultOutboundSubject(matter) {
    const subject = String(matter.subject || matter.document_title || matter.source_filename || "NDA redline").trim();
    if (!subject) return "Re: NDA redline";
    return subject.toLowerCase().startsWith("re:") ? subject : `Re: ${subject}`;
  }

  function defaultOutboundBody(matter) {
    const subject = matter.subject || matter.document_title || matter.source_filename || "the NDA";
    return `Hi,\n\nPlease find attached the redlined version of ${subject}.\n\nBest,\nAspora Legal`;
  }

  function sendPayloadFromPanel(repositoryMatterPanel, matter) {
    const subject = repositoryMatterPanel?.querySelector("#repositorySendSubject")?.value || "";
    const body = repositoryMatterPanel?.querySelector("#repositorySendBody")?.value || "";
    const sendPayload = {
      matter_id: matter.id,
      confirm_send: true,
    };
    if (subject.trim()) sendPayload.subject = subject;
    if (body.trim()) sendPayload.body = body;
    return sendPayload;
  }

  return {
    defaultOutboundBody,
    defaultOutboundSubject,
    outboundAccountLabel,
    renderSendComposer,
    sendPayloadFromPanel,
  };
})();
