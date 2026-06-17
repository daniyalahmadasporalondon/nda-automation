const RepositorySend = (() => {
  function renderSendComposer({ confirmingSend, gmailStatus, matter, personalisation, recipient }) {
    if (!confirmingSend) return "";
    const subject = defaultOutboundSubject(matter);
    const body = defaultOutboundBody(matter, personalisation);
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
            <div>
              <dt>Attachment</dt>
              <dd>${escapeHtml(outboundAttachmentLabel(matter))}</dd>
            </div>
          </dl>
          ${renderChangeSummary(matter)}
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

  // The outbound redline filename + format the counterparty will receive. Derived
  // from the same redlineDownloadFilename rule the download path uses (a "-redlined.docx"
  // stem from the matter source), so the composer shows exactly what is attached.
  function outboundAttachmentLabel(matter) {
    const source = matter.source_filename || matter.document_title || "nda.docx";
    const filename = typeof redlineDownloadFilename === "function"
      ? redlineDownloadFilename(source)
      : "nda-redlined.docx";
    return `${filename} (Word)`;
  }

  // A short "Summary of changes" block, rendered ONLY from data already loaded into
  // the repository panel. The panel carries review findings (issue_count / the
  // review_result clauses) but NOT the effective redline edits or review comments
  // (those live on the separately fetched review payload's redline_draft), so we
  // surface the flagged-issue count and omit the redline/comment counts rather than
  // fabricate them (see report: backend plumbing follow-up).
  function renderChangeSummary(matter) {
    const reviewResult = matter.review_result || {};
    // Verdict gate (mirror repository-detail.js ~17-19): the flagged-issue count is
    // an AI verdict. A deterministic-only matter (ai_review_ran === false) must not
    // claim "Redline addresses N flagged clauses" — the AI never flagged them. Only
    // an explicit false suppresses; legacy payloads lacking the flag fall back to
    // "are there clauses" and keep the existing behavior.
    const aiReviewRan = typeof matter.ai_review_ran === "boolean"
      ? matter.ai_review_ran
      : (Array.isArray(reviewResult.clauses) && reviewResult.clauses.length > 0);
    if (!aiReviewRan) return "";
    const attentionCount = Array.isArray(reviewResult.clauses)
      ? reviewResult.clauses.filter((clause) => clause && clauseStatus(clause).requiresAttention).length
      : 0;
    const issueCount = Number(matter.issue_count || 0) || attentionCount;
    if (issueCount <= 0) return "";
    const noun = issueCount === 1 ? "clause" : "clauses";
    return `
          <section class="repository-send-summary" aria-label="Summary of changes">
            <p class="repository-send-summary-title">Summary of changes</p>
            <p class="repository-send-summary-line">Redline addresses ${issueCount} flagged ${noun}.</p>
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

  function defaultOutboundBody(matter, personalisation = null) {
    const subject = matter.subject || matter.document_title || matter.source_filename || "the NDA";
    return `Hi,\n\nPlease find attached the redlined version of ${subject}.\n\n${personalisationSignatureBlock(personalisation)}`;
  }

  function personalisationSignatureBlock(personalisation = null) {
    const signatureBlock = String(personalisation?.signature_block || "").trim();
    if (signatureBlock) return signatureBlock;
    const signOff = String(personalisation?.sign_off || "").trim();
    const signature = String(personalisation?.signature || "").trim();
    const parts = [signOff, signature].filter(Boolean);
    return parts.length ? parts.join("\n") : "Best,\nAspora Legal";
  }

  function sendPayloadFromPanel(repositoryMatterPanel, matter) {
    const subject = repositoryMatterPanel?.querySelector("#repositorySendSubject")?.value || "";
    const body = repositoryMatterPanel?.querySelector("#repositorySendBody")?.value || "";
    const sendPayload = {
      matter_id: matter.id,
      confirm_send: true,
      // Confirm the exact destination shown in the composer "To" field so a
      // spoofed inbound Reply-To cannot silently redirect the redline; the
      // server refuses to send when this does not match the resolved recipient.
      confirm_recipient: String(matter.recipient_email || ""),
    };
    if (subject.trim()) sendPayload.subject = subject;
    if (body.trim()) sendPayload.body = body;
    return sendPayload;
  }

  return {
    defaultOutboundBody,
    defaultOutboundSubject,
    outboundAccountLabel,
    personalisationSignatureBlock,
    renderSendComposer,
    sendPayloadFromPanel,
  };
})();
