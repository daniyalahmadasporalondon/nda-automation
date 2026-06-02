const MatterUtils = (() => {
  const EMAIL_PATTERN = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;

  function emailAddress(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const bracketed = text.match(/<([^<>]+)>/);
    const candidate = bracketed?.[1] || text;
    return candidate.match(EMAIL_PATTERN)?.[0] || "";
  }

  function sameEmailAddress(left, right) {
    return Boolean(left && right && String(left).trim().toLowerCase() === String(right).trim().toLowerCase());
  }

  function recipientEmail(matter) {
    return String(matter?.recipient_email || "");
  }

  function counterpartyEmail(matter, gmailStatus = {}) {
    const ownEmails = [
      matter?.gmail_account,
      gmailStatus?.inbound?.email,
      gmailStatus?.outbound?.email,
    ].map(emailAddress).filter(Boolean);
    const candidates = [
      matter?.recipient_email,
      matter?.reply_to,
      matter?.sender,
      matter?.last_outbound_to,
    ];
    for (const candidate of candidates) {
      const email = emailAddress(candidate);
      if (!email) continue;
      if (ownEmails.some((ownEmail) => sameEmailAddress(ownEmail, email))) continue;
      return email;
    }
    return "";
  }

  function canSendRedline(matter) {
    return Boolean(matter?.can_send_redline && recipientEmail(matter));
  }

  function gmailSendBlock(matter, gmailStatus = {}) {
    if (matter?.send_block_reason) return String(matter.send_block_reason);
    if (!canSendRedline(matter)) return "Matter does not have a valid reply recipient email address.";
    const outbound = gmailStatus?.outbound || {};
    if (outbound.enabled === false) return "Gmail outbound is disabled in Admin.";
    if (outbound.ready === false) return outbound.error || "Outbound Gmail is not ready.";
    const recipient = recipientEmail(matter).trim().toLowerCase();
    const ownEmails = [
      matter?.gmail_account,
      gmailStatus?.inbound?.email,
      outbound.email,
    ].map((email) => String(email || "").trim().toLowerCase()).filter(Boolean);
    if (recipient && ownEmails.includes(recipient)) {
      return `Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline back to ${recipient}.`;
    }
    const matterInbound = String(matter?.gmail_account || "").trim().toLowerCase();
    const outboundEmail = String(outbound.email || "").trim().toLowerCase();
    if (matterInbound && outboundEmail && matterInbound !== outboundEmail) {
      return `Outbound Gmail account ${outbound.email} does not match inbound Gmail account ${matter.gmail_account}.`;
    }
    return "";
  }

  function gmailSendButtonLabel(blockReason) {
    if (!blockReason) return "";
    if (blockReason.includes("disabled")) return "Outbound Off";
    if (blockReason.includes("does not match")) return "Account Mismatch";
    if (blockReason.includes("self-sent")) return "Self-Sent";
    if (blockReason.includes("sender") || blockReason.includes("reply recipient")) return "No Reply";
    return "Gmail Setup";
  }

  return { canSendRedline, counterpartyEmail, gmailSendBlock, gmailSendButtonLabel, recipientEmail };
})();
