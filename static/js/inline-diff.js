function renderInlineDiff(original, replacement) {
  const oldTokens = tokenizeInlineDiff(original);
  const newTokens = tokenizeInlineDiff(replacement);
  if (!oldTokens.length) return renderInlineTokens(newTokens, "inline-ins");
  if (!newTokens.length) return renderInlineTokens(oldTokens, "inline-del");
  if (oldTokens.length * newTokens.length > INLINE_DIFF_MAX_MATRIX_CELLS) {
    return `${renderInlineTokens(oldTokens, "inline-del")}${renderInlineTokens(newTokens, "inline-ins")}`;
  }

  return renderDiffOperations(diffTokenOperations(oldTokens, newTokens));
}

function tokenizeInlineDiff(text) {
  return String(text || "").match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[^\sA-Za-z0-9]/g) || [];
}

function diffTokenOperations(oldTokens, newTokens) {
  const rowCount = oldTokens.length + 1;
  const columnCount = newTokens.length + 1;
  const dp = Array.from({ length: rowCount }, () => new Array(columnCount).fill(0));

  for (let oldIndex = oldTokens.length - 1; oldIndex >= 0; oldIndex -= 1) {
    for (let newIndex = newTokens.length - 1; newIndex >= 0; newIndex -= 1) {
      dp[oldIndex][newIndex] = oldTokens[oldIndex] === newTokens[newIndex]
        ? dp[oldIndex + 1][newIndex + 1] + 1
        : Math.max(dp[oldIndex + 1][newIndex], dp[oldIndex][newIndex + 1]);
    }
  }

  const operations = [];
  let oldIndex = 0;
  let newIndex = 0;
  while (oldIndex < oldTokens.length && newIndex < newTokens.length) {
    if (oldTokens[oldIndex] === newTokens[newIndex]) {
      operations.push({ type: "same", token: oldTokens[oldIndex] });
      oldIndex += 1;
      newIndex += 1;
    } else if (dp[oldIndex + 1][newIndex] >= dp[oldIndex][newIndex + 1]) {
      operations.push({ type: "delete", token: oldTokens[oldIndex] });
      oldIndex += 1;
    } else {
      operations.push({ type: "insert", token: newTokens[newIndex] });
      newIndex += 1;
    }
  }
  while (oldIndex < oldTokens.length) {
    operations.push({ type: "delete", token: oldTokens[oldIndex] });
    oldIndex += 1;
  }
  while (newIndex < newTokens.length) {
    operations.push({ type: "insert", token: newTokens[newIndex] });
    newIndex += 1;
  }
  return operations;
}

function renderDiffOperations(operations) {
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

function renderInlineTokens(tokens, className) {
  let previousToken = "";
  return tokens
    .map((token) => {
      const prefix = needsInlineSpace(previousToken, token) ? " " : "";
      const html = className
        ? renderInlineToken(`${prefix}${token}`, className)
        : `${prefix}${renderInlineToken(token, className)}`;
      previousToken = token;
      return html;
    })
    .join("");
}

function renderInlineToken(token, className) {
  const escapedToken = escapeHtml(token);
  return className ? `<span class="${className}">${escapedToken}</span>` : escapedToken;
}

function needsInlineSpace(previousToken, token) {
  if (!previousToken) return false;
  if (/^[,.;:!?%)]$/.test(token)) return false;
  if (/^[(]$/.test(previousToken)) return false;
  return true;
}
