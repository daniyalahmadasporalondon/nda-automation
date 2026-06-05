// Resolve a friendly dashboard greeting from whatever identity the app has loaded.
//
// Priority: a real display name from the auth user, else the user's email, else
// the connected Gmail account email. We derive a first name from an email
// local-part ("daniyal.ahmad@x.com" -> "Daniyal"). When nothing usable exists we
// fall back to a plain "Welcome back" with no placeholder — never "Counsel".

// Title-case a single name token: "daniyal" -> "Daniyal", "o'brien" -> "O'Brien".
function titleCaseToken(token) {
  return String(token || "")
    .toLowerCase()
    .replace(/(^|[-'])([a-z])/g, (_, sep, ch) => sep + ch.toUpperCase());
}

// First name from an email local-part. "daniyal.ahmad" -> "Daniyal";
// "john_smith" -> "John"; "jdoe" -> "Jdoe". Drops plus-addressing and digits-only
// handles. Returns "" when nothing name-like can be derived.
function firstNameFromEmail(email) {
  const text = String(email || "").trim().toLowerCase();
  const at = text.indexOf("@");
  if (at <= 0) return "";
  let local = text.slice(0, at).split("+")[0];
  // Split on common separators; take the first meaningful chunk.
  const first = local.split(/[.\-_]/).filter(Boolean)[0] || local;
  // A purely numeric or single-character handle isn't a meaningful name.
  if (!first || first.length < 2 || /^\d+$/.test(first)) return "";
  return titleCaseToken(first);
}

// A real display name only if it isn't just the email/id echoed back (the basic /
// local session sets name === id === email). Returns the first word, title-cased.
function firstNameFromDisplayName(name, { email, id } = {}) {
  const text = String(name || "").trim();
  if (!text) return "";
  if (text.includes("@")) return "";
  const lower = text.toLowerCase();
  if (email && lower === String(email).trim().toLowerCase()) return "";
  if (id && lower === String(id).trim().toLowerCase()) return "";
  const firstWord = text.split(/\s+/)[0];
  return titleCaseToken(firstWord);
}

// Best first name from the available identity sources, or "" if none.
function resolveFirstName({ user = null, gmailStatus = null } = {}) {
  const u = user && typeof user === "object" ? user : {};
  const fromName = firstNameFromDisplayName(u.name, { email: u.email, id: u.id });
  if (fromName) return fromName;
  const fromUserEmail = firstNameFromEmail(u.email);
  if (fromUserEmail) return fromUserEmail;
  const gmail = gmailStatus && typeof gmailStatus === "object" ? gmailStatus : {};
  const gmailEmail = gmail?.inbound?.email || gmail?.outbound?.email || "";
  const fromGmail = firstNameFromEmail(gmailEmail);
  if (fromGmail) return fromGmail;
  return "";
}

// The full greeting string for the hero. Falls back to a plain, placeholder-free
// "Welcome back" — never the old "Counsel" stand-in.
function dashboardGreeting({ user = null, gmailStatus = null } = {}) {
  const firstName = resolveFirstName({ user, gmailStatus });
  return firstName ? `Welcome back, ${firstName}` : "Welcome back";
}

export {
  dashboardGreeting,
  firstNameFromEmail,
  firstNameFromDisplayName,
  resolveFirstName,
  titleCaseToken,
};
