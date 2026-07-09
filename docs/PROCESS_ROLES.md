# Process roles: the web / worker split (`NDA_PROCESS_ROLE`)

Why this exists: a Gmail trawl / AI import storm must never starve the UI (the
"Atul brick" incident class). The split moves the inbound pipeline driver — the
Gmail sync scheduler (poll → triage → import → cost-capped AI intake, plus the
hourly archive-rotation disk janitor riding its loop) — into its own process,
so web request threads never share a container with bulk background work.

## The roles

`NDA_PROCESS_ROLE` (parsed in `app_settings.process_role()`):

| Role | HTTP server | Gmail sync scheduler | Notes |
| --- | --- | --- | --- |
| `all` (default / unset) | Full app | Yes | Exactly today's single-process behavior. **Render and local dev use this — no change needed.** |
| `web` | Full app | **Never starts** | Serves all routes, including admin-triggered background jobs (garble backfill, pdf-docx backfill) — those stay runnable for `all`-parity but log a hint that the worker is the better home. |
| `worker` | **None** — a minimal listener serving ONLY `/healthz` (everything else 404) on `--port` (default 8787) | Yes | k8s liveness probe target. `/healthz` is honest: **503** (JSON `reason`) when the scheduler thread never started or died — the worker's entire job is that loop, so k8s restarts it instead of keeping a permanently-"healthy" dead worker. (Web/all `/healthz` stays always-200 by design: it gates deploys, see `server._send_healthz`.) Exits cleanly on SIGTERM (no scheduler stop-event exists; the daemon thread dies with the process — safe because store writes are atomic + flock-serialized and interrupted reviews heal at next boot). |

An **invalid** value refuses to boot with a clear error (it does not silently
default): a typo like `webb` falling back to `all` would start a second Gmail
poller inside the web container — the exact failure class the split prevents.
A crashing container is the loud, k8s-native signal.

## Single-poller guarantee

Exactly **one** container per data volume may run a scheduler-starting role
(`worker` or `all`). The pod spec pinning one worker container IS the
guarantee — no leader election. Defense-in-depth: each sync step also takes a
non-blocking `flock` on `NDA_DATA_DIR/gmail_sync.lock`
(`server._gmail_sync_process_lock`), so even a mis-deployed second poller on
the same volume cannot run a sync step concurrently.

## Why sharing one PVC is safe

Both containers mount the same volume at `NDA_DATA_DIR`. The matter store
serializes every read-modify-write under an exclusive `fcntl.flock` on
`NDA_DATA_DIR/matters.lock` **in addition to** its in-process `RLock`
(`matter_store._locked_store`), which is cross-process-safe on a single host /
shared filesystem — two containers in one pod behave like two threads do
today. The `list_matters` read cache is coherent across processes by
construction: it re-stats every record file on every read
(`matter_store._records_dir_fingerprint`), so a worker-side write is seen by
the web process's very next list read — no TTL, no staleness window beyond
nanosecond-mtime+size stat granularity. The settings read cache is likewise
stat-fingerprinted. Per-process render/list caches are read-path
optimizations only.

Two whole-file writers were hardened FOR this split (they were in-process-safe
only):

* **Gmail processed ledger** (`gmail_processed_ledger.py`): session/one-shot
  flushes now MERGE with the re-read on-disk state under an `fcntl` flock on
  `<ledger>.lock`, so the worker's poll flush, a web manual import
  (`POST /api/gmail/import`), and the web bulk-archive re-import guard can
  never erase each other's marks (which would mean duplicate imports, re-spent
  AI intake, and bulk-archived junk resurrecting).
* **Gmail inbound drain cursor** (`matter_store.advance_gmail_inbound_cursor`
  / `reset_gmail_inbound_cursor`): the read-compare-write now also runs under
  an flock on `gmail_inbound_cursors.json.lock` (the in-process RLock is
  kept), so interleaved cross-process advances cannot clobber another owner's
  cursor.

This does NOT hold across hosts or separate volumes — each instance remains a
data island (one PVC, one pod).

## EKS pod sketch (illustrative — lives in IT's manifests, not this repo)

```yaml
# Two containers, ONE pod, ONE shared PVC. Same image; only env differs.
spec:
  containers:
    - name: web
      image: <nda-automation-image>
      env:
        - { name: NDA_PROCESS_ROLE, value: "web" }
        - { name: NDA_DATA_DIR, value: /var/data }
        - { name: PORT, value: "8787" }   # Dockerfile CMD passes --port $PORT
      ports: [{ containerPort: 8787 }]    # Service/Ingress targets THIS one
      livenessProbe:
        httpGet: { path: /healthz, port: 8787 }
      readinessProbe:
        httpGet: { path: /readyz, port: 8787 }
      volumeMounts:
        - { name: nda-data, mountPath: /var/data }
    - name: worker
      image: <nda-automation-image>
      env:
        - { name: NDA_PROCESS_ROLE, value: "worker" }
        - { name: NDA_DATA_DIR, value: /var/data }
        - { name: PORT, value: "8788" }   # healthz-only listener
      livenessProbe:
        httpGet: { path: /healthz, port: 8788 }
      volumeMounts:
        - { name: nda-data, mountPath: /var/data }
  volumes:
    - name: nda-data
      persistentVolumeClaim: { claimName: nda-data }
```

Do not expose the worker's port via any Service — `/healthz` is its only
route and everything else 404s, but it has no auth stack at all.

## Render / dev

Unchanged. `NDA_PROCESS_ROLE` unset ⇒ `all` ⇒ byte-for-byte today's startup
path (`python -m nda_automation.server`).
