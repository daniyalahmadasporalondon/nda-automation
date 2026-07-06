# NDA Automation — Scale-Readiness for the EKS Migration

*Prepared for IT / Platform. Plain-English summary of what has to change before this service can scale up and run as multiple copies on Kubernetes (EKS).*

## Executive summary

The NDA Automation app works well today, but it was built to run as **a single copy on one machine**, and it keeps all of its data as **files on that machine's local disk**, guarded by **one shared lock** and served from **in-memory caches**. That single design decision is the root cause of nearly everything below: it creates "data-growth cliffs" (the app slows down or runs out of memory as the number of matters and users grows) **and** it prevents us from running a second copy for reliability or load (the disk can only attach to one machine, and each copy would keep its own separate counters and caches).

**The one fix that unlocks everything: move storage off the local disk — matters into a real database, documents into object storage (e.g. S3), and shared state/coordination into Redis.** That single change relieves the growth cliffs *and* makes it safe to run multiple copies on EKS. Until then, we can buy meaningful headroom with a few low-risk "bigger box" fixes — and we should fix one data-loss bug immediately, because it can bite us well before we ever "scale."

## What breaks as we grow (still on one box)

| Issue | In plain English | Rough threshold |
|---|---|---|
| One global data lock | Every read blocks every write and vice-versa, so users wait on each other. | ~5–10 people at once; hard wall at ~10–30k matters |
| No real web server / no request timeout | Unlimited requests pile up with nothing to cut off a slow one; the box gets swamped. | ~50–100 people at once |
| PDF-to-image conversion uncapped | A few big PDFs at the same time can exhaust memory and crash the app. | ~4–6 large PDFs at once |
| Document converter over-books memory | Each conversion is allowed up to ~4 GiB on a 2 GB box — two at once can't fit. | ~2 conversions at once |
| **Auto-cleanup deletes open matters** 🚩 | Once there are 250 matters, the oldest are auto-deleted — **including live, in-progress ones. This is silent data loss.** | **250 matters (fix now, not later)** |
| Admin backup loads everything | "Export/backup" pulls the entire dataset into memory at once and can run the box out of memory. | ~8–10k matters |
| Slow bulk import | Importing many files at once gets quadratically slower (each new one re-checks all the others). | Large batch imports |
| Slow duplicate scan | The corpus "find duplicates" check compares everything against everything. | ~10–20k matters |
| Disk fills up forever | Leftover documents, caches, and temp files are never fully cleaned; the 1 GB disk only grows. | Ongoing / creeps up |
| Email polling never stops | Checking mailboxes has no time limit, so one slow mailbox can stall the cycle. | ~10–30 mailboxes |
| AI review has no deadline | If an AI provider is slow, a review can hang indefinitely and leak memory. | Any slow-provider event |
| Very long documents | Reviewing very long contracts slows down disproportionately with length. | Very long docs |

**Everyday example:** with ~10 people using it during a busy afternoon, requests start queuing behind the single lock and the app feels sluggish — not because the work is hard, but because everyone is waiting in line for the same file.

## EKS multi-replica blockers

Running a second copy ("replica") on EKS is what gives us reliability and lets us handle more load. A few things must change first, but the scope is contained.

| Blocker | In plain English |
|---|---|
| **Single-attach disk (the gate)** | **The local disk can only be attached to one machine at a time, so a second replica cannot even start today. This is the hard gate — nothing else about multi-replica matters until storage moves off local disk.** |
| Duplicate paid AI reviews | The "don't review the same thing twice" guard lives in one copy's memory, so two copies would each pay to review the same document. |
| Email double-polling | The mailbox scheduler starts inside every copy, so two copies would fetch and process the same emails twice. |
| Rate limits doubled | Request limits are counted per-copy, so two copies effectively double the intended limits. |
| Email ledger not shared-safe | The "already processed this email" record isn't safe for two copies to write at once. |

**Reassuring — session affinity is NOT a blocker.** Logins, sessions, OAuth, and document render-status already share the disk and recover cleanly, so we do **not** need to pin a user to a specific copy. The multi-replica work is a **contained storage/coordination change, not a rewrite of the request-handling layer.**

## Recommended sequence

**Phase 1 — Quick wins (low risk, buy headroom on the current single box):**
1. **Fix the open-matter deletion bug first** — stop auto-cleanup from ever deleting active/open matters. This is data loss and can happen at just 250 matters, so it's independent of scale.
2. **Give it a bigger box** — more memory and CPU immediately relieve the memory-overcommit and OOM risks.
3. **Run behind a real web server** with request limits and timeouts, so slow or excess requests get cut off instead of piling up.
4. **Cap PDF-to-image conversion** so a burst of large PDFs can't exhaust memory.
5. **Add real disk cleanup** for orphaned documents, caches, and temp files (capped by actual size, not just count).

**Phase 2 — The real unlock (enables true scale + EKS multi-replica):**
6. **Move storage off local disk:** matters into a **real database**, documents into **object storage (S3)**, and shared counters/locks/schedulers into **Redis**.
   - This removes the single global lock and the data-growth cliffs (Group A).
   - It removes the single-attach-disk gate and lets multiple replicas run safely — de-duplicated AI reviews, single email poller, shared rate limits, shared email ledger (Group B).

**Bottom line:** Phase 1 is a few days of low-risk hardening that also closes a real data-loss hole. Phase 2 — the storage move — is the single change that both removes the growth cliffs and makes the EKS multi-replica migration safe. Everything else is downstream of that one decision.
