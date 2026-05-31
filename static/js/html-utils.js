function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function joinClasses(...classes) {
  return classes.flat().filter(Boolean).join(" ");
}

function mergeClauses(primaryClauses, secondaryClauses) {
  const merged = [...primaryClauses];
  secondaryClauses.forEach((clause) => {
    if (!merged.find((item) => item.id === clause.id)) merged.push(clause);
  });
  return merged;
}
