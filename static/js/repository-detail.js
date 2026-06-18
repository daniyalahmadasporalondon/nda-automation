const RepositoryDetail = (() => {
  function renderDetailPanel({
    handlers,
    matter,
    pendingSendMatterId,
    repositoryMatterPanel,
    repositoryWorkspace,
    state,
  }) {
    if (!repositoryMatterPanel) return;
    const reviewResult = matter.review_result || {};
    // Only an AI review (ai_review_ran) may surface verdicts/checks/issues. A
    // deterministic-only matter shows a "Review not run -- Refresh with AI" Pending
    // state instead of the deterministic counts (the last "deterministic ghost").
    // Triage metadata (route/counterparty/dedup) is untouched. Fall back to "are
    // there clauses" only for fixtures predating the flag.
    const aiReviewRan = typeof matter.ai_review_ran === "boolean"
      ? matter.ai_review_ran
      : (Array.isArray(reviewResult.clauses) && reviewResult.clauses.length > 0);
    const reviewNotRunLabel = "Review not run";
    const attentionClauses = aiReviewRan && Array.isArray(reviewResult.clauses)
      ? reviewResult.clauses.filter((clause) => clause && clauseStatus(clause).requiresAttention)
      : [];
    const subject = RepositoryModel.matterSubject(matter);
    const fileName = matter.source_filename || matter.attachment_filename || subject;
    const recipient = MatterUtils.recipientEmail(matter);
    const sendBlockReason = MatterUtils.gmailSendBlock(matter, state.gmailStatus);
    const sendBlockLabel = MatterUtils.gmailSendButtonLabel(sendBlockReason);
    const canSendRedline = !sendBlockReason;
    const confirmingSend = pendingSendMatterId === matter.id;
    repositoryMatterPanel.hidden = false;
    repositoryWorkspace?.classList.remove("detail-open");
    repositoryMatterPanel.setAttribute("aria-label", `Matter inspector for ${fileName}`);
    repositoryMatterPanel.innerHTML = `
      <section class="repository-inspector-dialog" aria-labelledby="repositoryInspectorTitle">
        <header class="repository-detail-head">
          <div class="repository-inspector-heading">
            <span class="repository-inspector-icon" aria-hidden="true"></span>
            <div>
              <p class="repository-detail-kicker">${escapeHtml(RepositoryModel.sourceTypeLabel(matter.source_type))}</p>
              <h2 id="repositoryInspectorTitle">${escapeHtml(fileName)}</h2>
            </div>
          </div>
          <button class="repository-detail-close" type="button" aria-label="Close matter inspector">x</button>
        </header>

        <div class="repository-inspector-body">
          <section class="repository-inspector-main" aria-label="Matter review details">
            ${MatterUtils.reviewStale(matter) ? `
            <section class="repository-stale-notice" role="status">
              <span class="repository-stale-badge">Stale</span>
              <p>${escapeHtml(MatterUtils.reviewStaleLabel(matter))}</p>
            </section>` : ""}
            <section class="repository-inspector-section">
              <p class="repository-inspector-section-title">Metadata Details</p>
              <dl class="repository-detail-meta repository-detail-meta-grid">
                ${renderInspectorField("File name", fileName)}
                ${renderInspectorField("Subject", subject)}
                ${renderInspectorField("Status", RepositoryModel.statusLabel(matter))}
                ${renderInspectorField("Review route", RepositoryModel.triageLabel(matter.triage_status))}
                ${renderInspectorField("Date ingested", RepositoryModel.formatMatterDateTime(matter.created_at) || "-")}
                ${renderInspectorField("Last updated", RepositoryModel.formatMatterDateTime(matter.updated_at) || "-")}
              </dl>
            </section>

            <section class="repository-inspector-section">
              <p class="repository-inspector-section-title">Review Checks</p>
              <div class="repository-check-grid">
                <div class="repository-check-card">
                  <span>Pass checks</span>
                  <strong>${aiReviewRan ? RepositoryModel.reviewCountSummary(matter, reviewResult) : escapeHtml(reviewNotRunLabel)}</strong>
                </div>
                <div class="repository-check-card">
                  <span>Playbook match</span>
                  <strong>${aiReviewRan ? escapeHtml(RepositoryModel.playbookMatchLabel(matter, reviewResult)) : escapeHtml(reviewNotRunLabel)}</strong>
                </div>
                <div class="repository-check-card">
                  <span>Issues</span>
                  <strong>${aiReviewRan ? `${Number(matter.issue_count || 0)} ${Number(matter.issue_count || 0) === 1 ? "issue" : "issues"}` : escapeHtml(reviewNotRunLabel)}</strong>
                </div>
                <div class="repository-check-card">
                  <span>Redline draft</span>
                  <strong>${escapeHtml(matter.has_redline_draft ? "Draft redline saved" : "No custom draft")}</strong>
                </div>
              </div>
              ${aiReviewRan ? "" : `<p class="repository-review-pending">Refresh with AI to run a review and see verdicts.</p>`}
            </section>

            ${aiReviewRan ? `
            <section class="repository-detail-issues">
              <h3>Key Failed Clauses</h3>
              ${renderFailedClauses(attentionClauses)}
            </section>` : ""}

            ${RepositorySend.renderSendComposer({
              confirmingSend,
              gmailStatus: state.gmailStatus,
              matter,
              personalisation: state.personalisationSettings,
              recipient,
            })}
          </section>

          <aside class="repository-inspector-side" aria-label="Matter routing and timeline">
            <section class="repository-inspector-section">
              <p class="repository-inspector-section-title">Gmail Routing</p>
              <dl class="repository-detail-email">
                ${renderInspectorField("From", RepositoryModel.matterSender(matter))}
                ${renderInspectorField("Inbound mailbox", matter.gmail_account || "Manual repository intake")}
                ${renderInspectorField("Outbound status", sendBlockReason || "Ready")}
                ${renderInspectorField("Reply to", recipient || "No reply address detected")}
                ${renderInspectorField("Received", RepositoryModel.formatMatterDateTime(matter.received_at || matter.created_at) || "-")}
                ${renderInspectorField("Attachment", matter.attachment_filename || matter.source_filename || "-")}
                ${matter.last_outbound_at ? renderInspectorField("Last sent", RepositoryModel.formatMatterDateTime(matter.last_outbound_at) || "-") : ""}
                ${matter.last_outbound_account ? renderInspectorField("Last sent from", matter.last_outbound_account) : ""}
                ${matter.last_outbound_to ? renderInspectorField("Last sent to", matter.last_outbound_to) : ""}
              </dl>
              <p class="repository-message-preview">${escapeHtml(matter.message_snippet || "No message preview available.")}</p>
            </section>

            <section class="repository-inspector-section">
              <p class="repository-inspector-section-title">Signatures</p>
              ${renderInspectorSignatures(matter)}
            </section>

            <section class="repository-inspector-section">
              <p class="repository-inspector-section-title">Matter Timeline</p>
              ${renderMatterTimeline(matter)}
            </section>

            ${renderDriveArchiveWarning(matter)}
            ${renderDriveFolder(matter)}
          </aside>
        </div>

        <footer class="repository-inspector-footer">
          <p class="repository-detail-message" aria-live="polite"></p>
          <div class="repository-detail-actions">
            <button type="button" class="repository-open-review">Open Review</button>
            ${MatterUtils.reviewActionable(matter) ? `<button type="button" class="secondary repository-refresh-review">${MatterUtils.reviewNeverRan(matter) ? "Run AI review" : "Refresh Review"}</button>` : ""}
            <button type="button" class="secondary repository-download-document" aria-haspopup="menu" aria-expanded="false">Download</button>
            <button type="button" class="secondary repository-save-to-drive">Save to Drive</button>
            <button type="button" class="secondary repository-send-redline ${confirmingSend ? "confirming" : ""}" ${canSendRedline ? "" : "disabled"} title="${escapeHtml(sendBlockReason)}">
              <span class="send-plane-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" focusable="false">
                  <path d="M21 3 10.8 13.2"/>
                  <path d="M21 3 14.5 21 10.8 13.2 3 10.5 21 3Z"/>
                </svg>
              </span>
              <span>${sendBlockReason ? escapeHtml(sendBlockLabel) : confirmingSend ? "Confirm Send" : "Send Redline"}</span>
            </button>
          </div>
        </footer>
      </section>
    `;
    repositoryMatterPanel.querySelector(".repository-detail-close")?.addEventListener("click", handlers.closePanel);
    repositoryMatterPanel.querySelector(".repository-open-review")?.addEventListener("click", () => handlers.openMatterInReview(matter));
    repositoryMatterPanel.querySelector(".repository-refresh-review")?.addEventListener("click", () => handlers.refreshMatterReview?.(matter));
    repositoryMatterPanel.querySelector(".repository-download-document")?.addEventListener("click", (event) => handlers.openDownloadMenu(matter, event.currentTarget));
    repositoryMatterPanel.querySelector(".repository-save-to-drive")?.addEventListener("click", () => handlers.saveMatterToDrive(matter));
    // Retry a failed executed-archive: reuse the deliberate Save-to-Drive / sync
    // path, which re-syncs the matter and (on success) clears the drive_archive
    // failure flag so this warning disappears on the refreshed render.
    repositoryMatterPanel.querySelector(".repository-drive-archive-retry")?.addEventListener("click", () => handlers.saveMatterToDrive(matter));
    repositoryMatterPanel.querySelector(".repository-send-redline")?.addEventListener("click", () => handlers.sendRedline(matter));
  }

  function renderEmptyPanel({ repositoryMatterPanel, repositoryWorkspace }) {
    if (!repositoryMatterPanel) return;
    repositoryWorkspace?.classList.remove("detail-open");
    repositoryMatterPanel.hidden = true;
    repositoryMatterPanel.innerHTML = '<div class="repository-detail-empty">Select a matter</div>';
  }

  function setPanelMessage(repositoryMatterPanel, message) {
    const messageNode = repositoryMatterPanel?.querySelector(".repository-detail-message");
    if (messageNode) messageNode.textContent = message;
  }

  // Render trusted HTML into the panel message (e.g. a "Saved to Drive" link or a
  // Connect Google Drive affordance). Callers must escape any untrusted values
  // before passing markup here.
  function setPanelMessageHtml(repositoryMatterPanel, html) {
    const messageNode = repositoryMatterPanel?.querySelector(".repository-detail-message");
    if (messageNode) messageNode.innerHTML = html;
  }

  function renderFailedClauses(clauses) {
    if (!clauses.length) {
      return '<p class="repository-detail-none">No clauses need attention</p>';
    }
    return `
      <ul>
        ${clauses.slice(0, 6).map((clause) => {
          const status = clauseStatus(clause);
          return `
            <li>
              <strong>${escapeHtml(clause.name || clause.id || "Clause")}</strong>
              <span>${escapeHtml(status.needsReview ? "Needs review" : clause.issue_label || clause.reason || "Needs review")}</span>
            </li>
          `;
        }).join("")}
      </ul>
    `;
  }

  // Surface the matter's Drive folder inline when the matter already carries a
  // drive block. With auto-intake on, the block is populated at creation (no
  // Save-to-Drive click needed); after a manual sync it is refreshed. Renders
  // nothing when the matter has not been filed to Drive yet.
  function renderDriveFolder(matter) {
    const drive = matter && matter.drive;
    const folderUrl = drive ? String(drive.matter_folder_url || "") : "";
    if (!folderUrl) return "";
    return `
      <section class="repository-inspector-section repository-drive-section">
        <p class="repository-inspector-section-title">Google Drive</p>
        <a class="repository-detail-link repository-drive-folder-link" href="${escapeHtml(folderUrl)}" target="_blank" rel="noopener">Open matter folder</a>
      </section>
    `;
  }

  // Non-blocking warning when an executed matter's Drive auto-archive FAILED. The
  // matter is still fully signed — the executed transition never blocks on Drive —
  // but the signed copy never reached Drive, which was previously silent. Renders
  // nothing unless the matter carries drive_archive.status === "failed" (a matter
  // with no Drive connection has no drive_archive block at all -> no false warning).
  // The Retry button re-attempts the archive via the Save-to-Drive / sync path.
  function renderDriveArchiveWarning(matter) {
    const archive = matter && matter.drive_archive;
    if (!archive || String(archive.status || "") !== "failed") return "";
    const reason = String(archive.error || "").trim();
    const detail = reason ? ` — ${escapeHtml(reason)}` : "";
    return `
      <section class="repository-inspector-section repository-drive-archive-warning" role="alert">
        <p class="repository-inspector-section-title">Drive archive failed</p>
        <p class="repository-drive-archive-warning-text">Signed, but the Drive archive failed${detail}. The signed copy did not reach Google Drive.</p>
        <button type="button" class="secondary repository-drive-archive-retry">Retry Drive archive</button>
      </section>
    `;
  }

  // SIGNATURES section for the inspector pop-up. Display-only: it reads the
  // matter's per-party e-signature state and prints exactly two binary lines —
  // "Aspora — Signed/Pending" and "Counterparty — Signed/Pending" (a declined
  // party reads "Declined"). It NEVER mutates state and computes nothing new: it
  // reuses the exact per-party model the Overview tab uses. We prefer the bridged
  // window.signatureParties (overview/signatures.js, the tested helper) when the
  // page has loaded it, and otherwise fall back to a behaviour-identical local
  // replica (inspectorSignatureParties) so the inspector and the Overview block
  // can never disagree. The status set is collapsed to the inspector's binary:
  // signed -> "Signed", declined -> "Declined", everything else -> "Pending".
  function renderInspectorSignatures(matter) {
    const partiesFn = (typeof window !== "undefined" && typeof window.signatureParties === "function")
      ? window.signatureParties
      : inspectorSignatureParties;
    const parties = partiesFn(matter || {});
    return `
      <dl class="repository-detail-meta repository-signatures-list">
        ${parties.map((party) => renderInspectorField(party.label, inspectorSignatureText(party.status))).join("")}
      </dl>
    `;
  }

  // Collapse the Overview party-model status to the inspector's binary surface.
  function inspectorSignatureText(status) {
    if (status === "signed") return "Signed";
    if (status === "declined") return "Declined";
    return "Pending";
  }

  // Behaviour-identical replica of overview/signatures.js signatureParties, used
  // only when the bridged window.signatureParties is not present. Always returns
  // exactly two parties in a stable order (Aspora, then Counterparty). Mirrors the
  // Overview logic: no envelope + matter-executed -> both signed (executed
  // off-platform); otherwise resolve each party from the matching stored signer by
  // role, with a missing signer / no envelope reading not_sent. Kept in lockstep
  // with the Overview block; the FE test asserts parity with the real helper.
  function inspectorSignatureParties(matter) {
    const docusign = matter && typeof matter.docusign === "object" ? matter.docusign : null;
    const signers = docusign && Array.isArray(docusign.signers) ? docusign.signers : [];
    const sent = !!(docusign && String(docusign.envelope_id || "").trim());

    if (!sent && inspectorMatterIsExecuted(matter)) {
      return [
        inspectorPartyModel("Aspora", { signature_status: "signed" }, true),
        inspectorPartyModel("Counterparty", { signature_status: "signed" }, true),
      ];
    }

    const aspora = signers.find((s) => s && s.role === "aspora") || null;
    const counterparty = signers.find((s) => s && s.role && s.role !== "aspora") || null;
    return [
      inspectorPartyModel("Aspora", aspora, sent),
      inspectorPartyModel("Counterparty", counterparty, sent),
    ];
  }

  function inspectorMatterIsExecuted(matter) {
    if (!matter || typeof matter !== "object") return false;
    if (matter.executed === true) return true;
    return String(matter.status || "").trim().toLowerCase() === "fully_signed";
  }

  function inspectorPartyModel(label, signer, sent) {
    if (!sent || !signer) {
      return { label, status: "not_sent", signedAt: "" };
    }
    return {
      label,
      status: inspectorNormalizeStatus(signer.signature_status),
      signedAt: String(signer.signed_at || "").trim(),
    };
  }

  function inspectorNormalizeStatus(value) {
    const v = String(value || "").trim().toLowerCase();
    if (v === "signed") return "signed";
    if (v === "declined") return "declined";
    return "awaiting";
  }

  function renderInspectorField(label, value) {
    const displayValue = value === undefined || value === null || value === "" ? "-" : String(value);
    return `
      <div>
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(displayValue)}</dd>
      </div>
    `;
  }

  function renderMatterTimeline(matter) {
    const reviewResult = matter.review_result || {};
    // Mirror the verdict gate in renderDetailPanel (lines ~17-19): only an AI
    // review may surface the "Playbook checks completed" count summary. A
    // deterministic-only matter has a checked_at but no AI verdict, so the count
    // tally below would be a "deterministic ghost". Legacy fixtures predating the
    // flag fall back to "are there clauses".
    const aiReviewRan = typeof matter.ai_review_ran === "boolean"
      ? matter.ai_review_ran
      : (Array.isArray(reviewResult.clauses) && reviewResult.clauses.length > 0);
    const events = [
      {
        detail: matter.attachment_filename || matter.source_filename || "Document received for review.",
        meta: RepositoryModel.formatMatterDateTime(matter.received_at || matter.created_at) || "-",
        title: `${RepositoryModel.sourceTypeLabel(matter.source_type)} intake`,
      },
    ];
    if (aiReviewRan && reviewResult.checked_at) {
      events.push({
        detail: `${RepositoryModel.reviewCountSummary(matter, reviewResult)}.`,
        meta: RepositoryModel.formatMatterDateTime(reviewResult.checked_at) || "-",
        title: "Playbook checks completed",
      });
    }
    if (matter.has_redline_draft) {
      events.push({
        detail: "Custom redline decisions are saved for this matter.",
        meta: RepositoryModel.formatMatterDateTime(matter.updated_at) || "-",
        title: "Redline draft saved",
      });
    }
    if (matter.last_outbound_at) {
      events.push({
        detail: `Sent to ${matter.last_outbound_to || "counterparty"} from ${matter.last_outbound_account || "outbound Gmail"}.`,
        meta: RepositoryModel.formatMatterDateTime(matter.last_outbound_at) || "-",
        title: "Outbound redline sent",
      });
    }
    events.push({
      detail: matter.next_action || "Review matter and decide next step.",
      meta: RepositoryModel.boardColumnLabel(matter.board_column),
      title: "Current next action",
    });

    return `
      <ol class="repository-timeline">
        ${events.map((event) => `
          <li>
            <div>
              <strong>${escapeHtml(event.title)}</strong>
              <span>${escapeHtml(event.meta)}</span>
            </div>
            <p>${escapeHtml(event.detail)}</p>
          </li>
        `).join("")}
      </ol>
    `;
  }

  return {
    renderDetailPanel,
    renderDriveArchiveWarning,
    renderDriveFolder,
    renderEmptyPanel,
    renderFailedClauses,
    renderInspectorField,
    renderInspectorSignatures,
    renderMatterTimeline,
    setPanelMessage,
    setPanelMessageHtml,
  };
})();
