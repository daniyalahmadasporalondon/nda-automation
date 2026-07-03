// Versioned specifier matching global-bridge.mjs's html-utils.mjs token exactly:
// same token -> same resolved URL -> single module instance, AND the browser can
// cache it immutably instead of revalidating a query-less URL on every visit.
// Keep this token in lockstep with html-utils.mjs's bytes (and with the token in
// global-bridge.mjs -- the manifest guard fails on a conflicting pair).
import { escapeHtml } from "./html-utils.mjs?v=20260703cachebust1";

export function fullReplacementOperations(original, replacement) {
  const operations = [];
  if (String(original || "")) operations.push({ type: "delete", token: String(original || "") });
  if (String(replacement || "")) operations.push({ type: "insert", token: String(replacement || "") });
  return operations;
}

export function renderDiffOperations(operations) {
  let previousOriginalToken = "";
  let previousAcceptedToken = "";
  // Defensive: drop any stray null/typeless op (and tolerate a non-array input)
  // so a single malformed diff operation that slipped the redline sanitizer can
  // never throw here and abort the surrounding document/clause render.
  return (Array.isArray(operations) ? operations : [])
    .filter((op) => op && op.type)
    .map((operation) => {
      const className = operation.type === "delete"
        ? "inline-del"
        : operation.type === "insert"
          ? "inline-ins"
          : "";
      if (operation.type === "delete") {
        const token = `${needsInlineSpace(previousOriginalToken, operation.token) ? " " : ""}${operation.token}`;
        previousOriginalToken = operation.token;
        return renderInlineToken(token, className);
      }
      if (operation.type === "insert") {
        const token = `${needsInlineSpace(previousAcceptedToken, operation.token) ? " " : ""}${operation.token}`;
        previousAcceptedToken = operation.token;
        return renderInlineToken(token, className);
      }
      const prefix = needsInlineSpace(previousOriginalToken, operation.token) || needsInlineSpace(previousAcceptedToken, operation.token)
        ? " "
        : "";
      previousOriginalToken = operation.token;
      previousAcceptedToken = operation.token;
      return `${prefix}${renderInlineToken(operation.token, className)}`;
    })
    .join("");
}

export function renderInlineToken(token, className) {
  const escapedToken = escapeHtml(token);
  return className ? `<span class="${className}">${escapedToken}</span>` : escapedToken;
}

export function needsInlineSpace(previousToken, token) {
  if (!previousToken) return false;
  if (/^\s/.test(token) || /\s$/.test(previousToken)) return false;
  const tokenCore = String(token).trimStart();
  const previousCore = String(previousToken).trimStart();
  if (/^[,.;:!?%)]$/.test(tokenCore)) return false;
  if (/^[(]$/.test(previousCore)) return false;
  if (/^[$£€#@]$/.test(previousCore) && /^\d/.test(tokenCore)) return false;
  // Email addresses: never split a word from an '@' or an '@' from a word
  // ('legal@discloser.com'). Mirrors _needs_inline_space in redline_xml.py so the
  // on-screen preview matches the exported DOCX byte-for-byte.
  if (tokenCore === "@" || previousCore === "@") return false;
  // URL path separators ('https://example.com/policy'): bare '/' tokens between
  // word/URL pieces stay tight.
  if (tokenCore === "/" || previousCore === "/") return false;
  // Dotted abbreviations ('U.S.', 'e.g.'): a '.' directly followed by a word token
  // with no source whitespace stays tight. A real sentence boundary ('end. Next')
  // carries its space on the following token and bails at the leading-whitespace
  // check above, so normal sentence spacing is preserved.
  if (previousCore === "." && /^(?:[^\W_]|\d)/u.test(tokenCore)) return false;
  return true;
}
