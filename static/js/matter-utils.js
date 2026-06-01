const MatterUtils = (() => {
  function recipientEmail(matter) {
    return String(matter?.recipient_email || "");
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

  return { canSendRedline, gmailSendBlock, gmailSendButtonLabel, recipientEmail };
})();
