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
//
// SELF-HEALING REDIRECT: a 401 on an *authenticated* request means the session
// died mid-use. Surfacing a banner alone leaves the app looking bricked, so the
// handler also redirects the browser to the login URL (preserving `next`) after
// a short beat — a dead session recovers instead of stranding the user.
//
// WHY THIS CAN'T LOOP: only requests that funnel through this handler trigger the
// redirect, and the auth/login/status endpoints never do. `/api/auth/status` is
// fetched directly in auth-session.js and handles its own 401 (authenticated:
// false) without calling here; the login endpoint is a navigation target, not a
// fetch. As a belt-and-braces guard we also refuse to redirect when we are
// already sitting on the login path, and a single `redirecting` latch means a
// burst of concurrent 401s schedules at most ONE navigation.
const AuthExpired = (() => {
  let notifyFn = null;
  let loginHref = "/auth/google/start";
  let prompting = false; // de-dupe concurrent 401s into a single prompt
  let redirecting = false; // fire at most ONE login redirect per expiry
  // Redirect delay: long enough for the "session expired" banner to register,
  // short enough that the user isn't left staring at a bricked screen.
  const REDIRECT_DELAY_MS = 1_200;
  // Seam so tests can observe the redirect without a real navigation (vm/jsdom
  // cannot assign window.location). Defaults to a real browser navigation.
  let redirectFn = (url) => {
    if (typeof window !== "undefined" && window.location) {
      window.location.assign ? window.location.assign(url) : (window.location.href = url);
    }
  };

  // Wire the handler to the app's notification system + login flow. Called once
  // from app.js. Kept tiny so app.js edits stay minimal.
  function register({ notify, loginHref: href, redirect } = {}) {
    if (typeof notify === "function") notifyFn = notify;
    if (typeof href === "string" && href) loginHref = href;
    if (typeof redirect === "function") redirectFn = redirect;
  }

  // True when the browser is already parked on the login path — redirecting again
  // would loop. Compares only the pathname of loginHref so query/`next` differ.
  function alreadyOnLoginPage() {
    if (typeof window === "undefined" || !window.location) return false;
    let loginPath = loginHref;
    try {
      // Resolve relative/absolute loginHref to a pathname for a clean compare.
      const base = window.location.origin || undefined;
      loginPath = new URL(loginHref, base).pathname;
    } catch {
      loginPath = String(loginHref).split("?")[0];
    }
    return window.location.pathname === loginPath;
  }

  function isAuthError(error) {
    return Boolean(error) && Number(error.status) === 401;
  }

  // Surface a clean "your session expired" prompt and self-heal by redirecting
  // to sign-in. De-duped so a burst of parallel 401s (the dashboard fires several
  // requests at load) shows a single prompt and schedules a single redirect, not
  // one per request.
  function handleAuthExpired() {
    // The redirect latch is the real single-fire guard: once a navigation is
    // scheduled, every later 401 in the burst is a no-op (prevents redirect
    // stacking / a flicker of repeated navigations).
    if (redirecting) return;
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
    scheduleLoginRedirect();
  }

  // Schedule the self-healing navigation to the login URL. Guarded so it fires at
  // most once per expiry and never when we're already on the login page (which
  // would loop). Runs after a short beat so the "session expired" banner lands
  // first. `redirecting` is intentionally NOT reset — once we've committed to
  // sending the browser to /login, the page is being torn down anyway.
  function scheduleLoginRedirect() {
    if (redirecting) return;
    if (alreadyOnLoginPage()) return; // belt-and-braces: never loop on /login
    redirecting = true;
    const target = loginUrl();
    const go = () => {
      try {
        redirectFn(target);
      } catch {
        /* navigation failed (e.g. test stub threw) — the banner still stands */
      }
    };
    if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
      window.setTimeout(go, REDIRECT_DELAY_MS);
    } else {
      go();
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
