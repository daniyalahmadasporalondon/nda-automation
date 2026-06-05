// Pure, testable helpers for the dashboard "Send Document" outbound flow.
// The browser controller (static/js/send-document.js) mirrors this logic; these
// exports are what the frontend tests exercise.

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isSupportedSendFilename(filename) {
  return /\.docx$/i.test(String(filename || ""));
}

export function isValidRecipientEmail(value) {
  return EMAIL_PATTERN.test(String(value || "").trim());
}

export function fileStem(filename) {
  return (
    String(filename || "")
      .split(/[\\/]/)
      .pop()
      .replace(/\.[^.]+$/, "") || "Document"
  );
}

// Returns { ok, error } describing whether the form can be submitted. Keeps the
// UI and the tests aligned on one source of validation truth.
export function validateSendDocument({ filename, hasFile, recipient } = {}) {
  if (!hasFile) {
    return { ok: false, error: "Attach a .docx document to send." };
  }
  if (!isSupportedSendFilename(filename)) {
    return { ok: false, error: "Attach a .docx Word document to send." };
  }
  if (!isValidRecipientEmail(recipient)) {
    return { ok: false, error: "Enter a valid recipient email address." };
  }
  return { ok: true, error: "" };
}

// Builds the JSON body for POST /api/send-document. Subject falls back to the
// file stem; empty optional fields are omitted so the backend applies defaults.
export function buildSendDocumentPayload({ filename, contentBase64, recipient, subject, body } = {}) {
  const payload = {
    filename: String(filename || ""),
    content_base64: String(contentBase64 || ""),
    to: String(recipient || "").trim(),
  };
  const cleanedSubject = String(subject || "").trim();
  payload.subject = cleanedSubject || fileStem(filename);
  const cleanedBody = String(body || "").trim();
  if (cleanedBody) {
    payload.body = cleanedBody;
  }
  return payload;
}
