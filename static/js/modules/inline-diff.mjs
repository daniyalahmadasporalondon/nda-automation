import { escapeHtml } from "./html-utils.mjs";

export function fullReplacementOperations(original, replacement) {
  const operations = [];
  if (String(original || "")) operations.push({ type: "delete", token: String(original || "") });
  if (String(replacement || "")) operations.push({ type: "insert", token: String(replacement || "") });
  return operations;
}

export function renderDiffOperations(operations) {
  let previousOriginalToken = "";
  let previousAcceptedToken = "";
  return operations
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
  return true;
}
