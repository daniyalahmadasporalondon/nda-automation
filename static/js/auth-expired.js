// Global session-expiry handling + a shared ok-before-parse JSON guard.
//
// THE BUG THIS FIXES: several fetch sites did `await response.json()` BEFORE
// checking `response.ok`. On session expiry the server returns a 401 with an
// empty or HTML body, so `response.json()` itself throws a SyntaxError whose
// Safari message is literally "The string did not match the expected pattern."
// That cryptic parse error masked the real cause (the session expired) and, in
// `loadMatters`, blanked the whole board.
//
// `parseOkJson` checks `response.ok` FIRST and only parses JSON when the
// response is ok. On a non-ok response it tries to read a structured error body
// but never lets a failed parse surface — it throws a clean Error carrying the
// real HTTP `status`. On a 401 it also fires the global auth-expired handler so
// the user gets a "session expired — sign in again" prompt instead of a cryptic
// failure.
const AuthExpired = (() => {
  let notifyFn = null;
  let loginHref = "/auth/google/start";
  let prompting = false; // de-dupe concurrent 401s into a single prompt

  // Wire the handler to the app's notification system + login flow. Called once
  // from app.js. Kept tiny so app.js edits stay minimal.
  function register({ notify, loginHref: href } = {}) {
    if (typeof notify === "function") notifyFn = notify;
    if (typeof href === "string" && href) loginHref = href;
  }

  function isAuthError(error) {
    return Boolean(error) && Number(error.status) === 401;
  }

  // Surface a clean "your session expired" prompt and offer a re-login path.
  // De-duped so a burst of parallel 401s (the dashboard fires several requests
  // at load) shows a single prompt, not one per request.
  function handleAuthExpired() {
    if (prompting) return;
    prompting = true;
    const message = "Your session expired — please sign in again.";
    let prompted = false;
    if (notifyFn) {
      try {
        notifyFn("Session expired", "Please sign in again to continue.");
        prompted = true;
      } catch {
        prompted = false;
      }
    }
    // Reset the de-dupe latch after a short window so a genuinely new expiry
    // later in the session can prompt again.
    window.setTimeout(() => { prompting = false; }, 8_000);
    if (!prompted && typeof window.alert === "function") {
      // Last-resort fallback if no notifier was registered.
      window.alert(message);
    }
  }

  // Build the login URL preserving where the user was so re-login returns here.
  function loginUrl() {
    const next = window.location.pathname + window.location.search;
    const separator = loginHref.includes("?") ? "&" : "?";
    return `${loginHref}${separator}next=${encodeURIComponent(next)}`;
  }

  // Shared ok-before-parse guard. `reviewErrorFromPayload` builds the descriptive
  // Error from a structured `{error, details, ...}` body; we always tag the
  // thrown Error with the real HTTP `status` so callers can branch on 401.
  async function parseOkJson(response, fallbackMessage, reviewErrorFromPayload) {
    if (response.ok) {
      return response.json();
    }
    // Non-ok: the body may be JSON ({error}) or an empty/HTML proxy page. Never
    // let a failed parse mask the underlying HTTP status with a SyntaxError.
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const error = typeof reviewErrorFromPayload === "function"
      ? reviewErrorFromPayload(payload, fallbackMessage)
      : new Error((payload && payload.error) || fallbackMessage);
    error.status = response.status;
    if (response.status === 401) handleAuthExpired();
    return Promise.reject(error);
  }

  return { register, isAuthError, handleAuthExpired, loginUrl, parseOkJson };
})();

if (typeof window !== "undefined") {
  window.AuthExpired = AuthExpired;
}

// Allow ESM consumers (repository-api.mjs is loaded as a module) to read the
// same singleton off window without a hard import dependency.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AuthExpired };
}
