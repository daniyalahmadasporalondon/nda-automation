const MatterUtils = (() => {
  function recipientEmail(matter) {
    return String(matter?.recipient_email || "");
  }

  function canSendRedline(matter) {
    return Boolean(matter?.can_send_redline && recipientEmail(matter));
  }

  return { canSendRedline, recipientEmail };
})();
