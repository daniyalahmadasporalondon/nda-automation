# Persistent Storage on Render

## Problem

The production service currently stores all application state in ephemeral `/tmp`:

```
NDA_DATA_DIR=/tmp/nda-automation/data
NDA_USERS_PATH=/tmp/nda-automation/data/users.json
NDA_EXPORTS_DIR=/tmp/nda-automation/exports
NDA_ALLOW_EPHEMERAL_DATA=true
```

`/tmp` is wiped on every Render redeploy and every idle spin-down (the free plan
spins down after ~15 minutes of inactivity). Consequences:

- **Every deploy logs all users out** (session tokens gone).
- **Every deploy loses all Google / Gmail OAuth tokens** — users must re-authenticate
  their Google connection after every deploy or spin-up.
- **Every deploy wipes all matters** — all reviewed NDAs, playbooks, and uploaded
  documents are permanently deleted.

## Feasibility

Render persistent disks require the **Starter plan or above**. The free plan
cannot mount a disk. The current `render.yaml` sets `plan: free`, so a disk
cannot be added without upgrading.

## Render Dashboard Steps (do these first)

1. Log in to [dashboard.render.com](https://dashboard.render.com) and open the
   **nda-automation** service.
2. Go to **Settings → Instance Type** and upgrade from **Free** to **Starter**
   (or higher). Starter costs $7/month and supports persistent disks.
3. Go to **Disks** (left sidebar) → **Add Disk**:
   - **Name:** `nda-data`
   - **Mount Path:** `/var/data`
   - **Size:** 1 GB (increase later if needed)
4. Click **Save Changes** — Render will trigger a redeploy with the disk mounted.
5. After the deploy completes, deploy or sync the `render.yaml` diff below so the
   env vars point at `/var/data`.

## render.yaml Diff to Apply

```diff
-    plan: free
+    # persistent disk requires Starter plan or above
+    plan: starter
+    disk:
+      name: nda-data
+      mountPath: /var/data
+      sizeGB: 1
     healthCheckPath: /healthz

-      - key: NDA_DATA_DIR
-        value: /tmp/nda-automation/data
-      - key: NDA_USERS_PATH
-        value: /tmp/nda-automation/data/users.json
-      - key: NDA_EXPORTS_DIR
-        value: /tmp/nda-automation/exports
-      - key: NDA_ALLOW_EPHEMERAL_DATA
-        value: "true"
+      - key: NDA_DATA_DIR
+        value: /var/data/nda-automation/data
+      - key: NDA_USERS_PATH
+        value: /var/data/nda-automation/data/users.json
+      - key: NDA_EXPORTS_DIR
+        value: /var/data/nda-automation/exports
```

The `NDA_ALLOW_EPHEMERAL_DATA=true` line is **removed** — its absence causes the
app to reject startup if the data directory is not writable, giving a loud error
rather than silently running with data loss.

All Google OAuth tokens are stored under `NDA_DATA_DIR/users/google/` and
`NDA_DATA_DIR/users/gmail/`, so repointing `NDA_DATA_DIR` covers them too.

## Branch Status

The prepared `render.yaml` (with the disk block and updated paths) is committed
on branch **`tm-persist-storage`** but **NOT pushed to `main`**, because pushing
a `disk:` block while the service is still on the free plan would cause the next
Render deploy to fail with a validation error.

**Apply order:**
1. Upgrade the Render instance type to Starter in the dashboard (step 2 above).
2. Then merge / push `tm-persist-storage` to `main` so the disk is declared in
   `render.yaml` consistently with the dashboard config.

## Why Not Push Now

Deploying a `render.yaml` with `disk:` while the service plan is `free` makes
Render reject the blueprint deploy (disk is a paid feature). The change is held
on the branch to protect the live deploy.
