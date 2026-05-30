const ndaText = document.querySelector("#ndaText");
const reviewButton = document.querySelector("#reviewButton");
const clearButton = document.querySelector("#clearButton");
const fileInput = document.querySelector("#fileInput");
const overallTitle = document.querySelector("#overallTitle");
const resultHero = document.querySelector("#resultHero");
const resultMark = document.querySelector("#resultMark");
const clauseGrid = document.querySelector("#clauseGrid");

const emptyState = () => {
  clauseGrid.innerHTML = '<div class="empty">No review yet</div>';
};

emptyState();

fileInput.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  ndaText.value = await file.text();
});

clearButton.addEventListener("click", () => {
  ndaText.value = "";
  overallTitle.textContent = "Awaiting review";
  resultMark.textContent = "-";
  resultHero.className = "result-hero";
  emptyState();
});

reviewButton.addEventListener("click", async () => {
  const text = ndaText.value.trim();
  if (!text) {
    overallTitle.textContent = "Add NDA text";
    resultMark.textContent = "-";
    resultHero.className = "result-hero fail";
    emptyState();
    return;
  }

  reviewButton.disabled = true;
  reviewButton.textContent = "Reviewing";

  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Review failed");
    renderResult(payload);
  } catch (error) {
    overallTitle.textContent = error.message;
    resultMark.textContent = "!";
    resultHero.className = "result-hero fail";
  } finally {
    reviewButton.disabled = false;
    reviewButton.textContent = "Review NDA";
  }
});

function renderResult(result) {
  const passed = result.overall_status === "meets_requirements";
  overallTitle.textContent = passed ? "Meets requirements" : "Does not meet requirements";
  resultMark.textContent = passed ? "PASS" : "FAIL";
  resultHero.className = `result-hero ${passed ? "pass" : "fail"}`;

  clauseGrid.innerHTML = result.clauses
    .map((clause) => {
      const evidence = clause.evidence.length
        ? `<p class="evidence">${escapeHtml(clause.evidence[0])}</p>`
        : "";
      return `
        <article class="clause-card">
          <header>
            <div>
              <h3>${escapeHtml(clause.name)}</h3>
              <p class="requirement">${escapeHtml(clause.requirement)}</p>
            </div>
            <span class="status ${clause.status}">${clause.status.toUpperCase()}</span>
          </header>
          <p class="finding">${escapeHtml(clause.finding)}</p>
          ${evidence}
        </article>
      `;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

