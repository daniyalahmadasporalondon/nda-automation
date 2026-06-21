const AdminHealthView = (() => {
  const STATUS_LABELS = { ok: "Healthy", warn: "Warning", alert: "Alert" };
  const STATUS_TONES = { ok: "ready", warn: "pending", alert: "blocked" };

  function createController({
    state,
    healthCard,
    healthFacts,
    healthStatus,
    healthAlerts,
    healthCaveat,
    healthRaw,
    healthRefreshButton,
    costTotal,
    costTokens,
    costFeatures,
    costCaveat,
    reviewErrorFromPayload,
  }) {
    healthRefreshButton?.addEventListener("click", load);

    async function load() {
      if (!healthCard) return;
      setStatus("checking", "Checking", true);
      try {
        const response = await fetch("/api/telemetry");
        const payload = await window.AuthExpired.parseOkJson(response, "AI review health could not load", reviewErrorFromPayload);
        render(payload);
      } catch (error) {
        renderError(error.message || "AI review health could not load");
      }
    }

    function render(payload = {}) {
      const health = payload.health || {};
      const telemetry = payload.telemetry || {};
      state.aiReviewHealth = health;

      const status = ["ok", "warn", "alert"].includes(health.status) ? health.status : "ok";
      setStatus(status, STATUS_LABELS[status] || "Healthy");

      renderAlerts(Array.isArray(health.alerts) ? health.alerts : []);

      const review = health.review || {};
      setFact("review-attempted", num(review.attempted));
      setFact("review-completed", num(review.completed));
      setFact("review-failed", num(review.failed));
      setFact("review-fail-closed", num(review.fail_closed));
      setFact("review-partial", num(review.partial));
      setFact("review-deterministic", num(review.deterministic_completed));
      setFact("review-fail-closed-rate", pct(review.fail_closed_rate));
      setFact("review-partial-rate", pct(review.partial_rate));

      const generation = health.generation || {};
      setFact("generation-requests", num(generation.requests));
      setFact("generation-succeeded", num(generation.succeeded));
      setFact("generation-rejected", num(generation.rejected));
      setFact("generation-failed", num(generation.failed));
      setFact("generation-gate-blocked", num(generation.safety_gate_blocked));
      setFact("generation-failure-rate", pct(generation.failure_rate));
      setFact("generation-gate-rate", pct(generation.gate_block_rate));

      setFact("other-failures", otherFailuresLabel(health.other || {}));

      renderCost(payload.ai_cost || {});

      setCaveat(caveatLabel(telemetry, health));
      setRaw(telemetry.counters || {});
    }

    function renderCost(cost = {}) {
      state.aiCost = cost;
      if (costTotal) costTotal.textContent = usd(cost.total_usd);
      if (costTokens) costTokens.textContent = `${num(cost.total_tokens)} tokens`;
      renderCostFeatures(Array.isArray(cost.features) ? cost.features : []);
      if (costCaveat) {
        costCaveat.textContent = cost.note
          || "Spend is cumulative since process start (since last restart); not windowed.";
      }
    }

    function renderCostFeatures(features) {
      if (!costFeatures) return;
      costFeatures.replaceChildren();
      if (!features.length) {
        const empty = document.createElement("p");
        empty.className = "health-caveat";
        empty.textContent = "No AI spend recorded since process start.";
        costFeatures.appendChild(empty);
        return;
      }
      features.forEach((row) => {
        const item = document.createElement("div");
        item.className = "ai-cost-feature";
        item.dataset.feature = String(row.feature || "unknown");

        const name = document.createElement("span");
        name.className = "ai-cost-feature-name";
        name.textContent = featureLabel(row.feature);

        const amount = document.createElement("span");
        amount.className = "ai-cost-feature-amount";
        amount.textContent = usd(row.cost_usd);

        const tokens = document.createElement("span");
        tokens.className = "ai-cost-feature-tokens";
        tokens.textContent = `${num(row.total_tokens)} tokens`;

        item.append(name, amount, tokens);
        costFeatures.appendChild(item);
      });
    }

    function featureLabel(value) {
      const raw = String(value || "unknown");
      return raw.replace(/[_.-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    }

    function renderError(message) {
      setStatus("alert", "Unavailable");
      renderAlerts([message]);
      [
        "review-attempted", "review-completed", "review-failed", "review-fail-closed",
        "review-partial", "review-deterministic", "review-fail-closed-rate", "review-partial-rate",
        "generation-requests", "generation-succeeded", "generation-rejected", "generation-failed",
        "generation-gate-blocked", "generation-failure-rate", "generation-gate-rate", "other-failures",
      ].forEach((key) => setFact(key, "—"));
      renderCost({ total_usd: 0, total_tokens: 0, features: [], note: message });
      setCaveat(message);
      setRaw({});
    }

    function renderAlerts(alerts) {
      if (!healthAlerts) return;
      healthAlerts.replaceChildren();
      const items = alerts.filter((alert) => typeof alert === "string" && alert.trim());
      if (!items.length) {
        const li = document.createElement("li");
        li.textContent = "No AI-review or generation failure thresholds crossed.";
        healthAlerts.appendChild(li);
        return;
      }
      items.forEach((alert) => {
        const li = document.createElement("li");
        li.textContent = alert;
        healthAlerts.appendChild(li);
      });
    }

    function setStatus(status, label, pending = false) {
      const tone = pending ? "pending" : (STATUS_TONES[status] || "ready");
      if (healthStatus) {
        healthStatus.textContent = label;
        healthStatus.dataset.healthStatus = status;
        healthStatus.classList.toggle("ready", tone === "ready");
        healthStatus.classList.toggle("blocked", tone === "blocked");
        healthStatus.classList.toggle("pending", tone === "pending");
      }
      if (healthAlerts) healthAlerts.dataset.healthStatus = status;
    }

    function setFact(key, value) {
      const node = healthCard?.querySelector(`[data-admin-health="${key}"]`)
        || healthFacts?.querySelector(`[data-admin-health="${key}"]`);
      if (node) node.textContent = value;
    }

    function setCaveat(value) {
      if (healthCaveat) healthCaveat.textContent = value;
    }

    function setRaw(counters) {
      if (!healthRaw) return;
      const entries = Object.entries(counters);
      if (!entries.length) {
        healthRaw.textContent = "No counters recorded since process start.";
        return;
      }
      healthRaw.textContent = entries
        .map(([key, value]) => `${key}: ${value}`)
        .join("\n");
    }

    function otherFailuresLabel(other) {
      const flagged = Object.entries(other).filter(([, value]) => Number(value) > 0);
      if (!flagged.length) return "None";
      // Humanize the raw counter key (`docx_export_content_failures` ->
      // "DOCX Export Content Failures") for this user-facing fact. The collapsible
      // "Raw counters" debug section below keeps the literal keys deliberately.
      const humanize = typeof window !== "undefined" && typeof window.humanizeCounterKey === "function"
        ? window.humanizeCounterKey
        : (value) => String(value);
      return flagged.map(([key, value]) => `${humanize(key)} ${value}`).join(", ");
    }

    function caveatLabel(telemetry, health) {
      const startedAt = telemetry.started_at ? friendlyTime(telemetry.started_at) : "process start";
      const uptime = formatUptime(telemetry.uptime_seconds);
      const note = health.note || "Counts are cumulative since process start.";
      return `Since ${startedAt}${uptime ? ` · uptime ${uptime}` : ""}. ${note}`;
    }

    function friendlyTime(value) {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }

    function formatUptime(seconds) {
      const total = Number(seconds);
      if (!Number.isFinite(total) || total <= 0) return "";
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      if (hours > 0) return `${hours}h ${minutes}m`;
      if (minutes > 0) return `${minutes}m`;
      return `${Math.floor(total)}s`;
    }

    function num(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? String(parsed) : "0";
    }

    function pct(value) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return "0%";
      return `${(parsed * 100).toFixed(1)}%`;
    }

    function usd(value) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed) || parsed < 0) return "$0.00";
      // Default to plain cents ($0.15, $12.40). Only widen to sub-cent precision when
      // a real spend would otherwise round AWAY to $0.00 — so small per-call costs
      // stay visible without making normal figures read as "$0.1500".
      const digits = parsed > 0 && parsed < 0.005 ? 4 : 2;
      return `$${parsed.toFixed(digits)}`;
    }

    // `render` is exposed alongside `load` so the payload renderers (including the
    // USD cost panel) can be driven directly in tests without a live fetch.
    return { load, render };
  }

  return { createController };
})();

function createAdminHealthController(options) {
  return AdminHealthView.createController(options);
}

// CommonJS export for the Node test harness (a no-op in the browser, mirroring
// admin-integrations.js). The page still consumes the IIFE globals above.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminHealthView, createAdminHealthController };
}
