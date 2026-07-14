# yoisho_bunny — Installation & Implementation Guide

Step-by-step guide for deploying the Bunny.net media offload app onto a Frappe LMS bench.  
Run these steps once per environment (local dev → staging → production).

**GitHub repo:** `https://github.com/AshwiniUpadhy/yoisho_bunny` (private)

---

## Prerequisites

- Frappe bench already set up with the LMS app installed and running
- SSH access to the server (needed once for installation; not needed for day-to-day operation after)
- Bunny.net account with two Storage Zones and two Pull Zones created (public + private)
- Bunny.net credentials ready (see **Step 3**)
- SSH deploy key added to the server (see **Step 1**)

---

## Step 1 — Set up SSH deploy key on the server (one-time per server)

The repo is private, so each server needs a deploy key to clone it.

**On the server:**

```bash
# Generate a deploy key (no passphrase)
ssh-keygen -t ed25519 -C "deploy-yoisho_bunny" -f ~/.ssh/id_ed25519_yoisho -N ""

# Print the public key — copy this
cat ~/.ssh/id_ed25519_yoisho.pub
```

**On GitHub:** Go to `github.com/AshwiniUpadhy/yoisho_bunny → Settings → Deploy keys → Add deploy key`  
Paste the public key. Read-only access is sufficient.

**Add an SSH alias on the server** — append to `~/.ssh/config`:

```
Host github-yoisho
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_yoisho
    IdentitiesOnly yes
```

**Test:**

```bash
ssh -T git@github-yoisho
# Expected: Hi AshwiniUpadhy! You've successfully authenticated...
```

---

## Step 2 — Install the app into the bench

SSH into the server and run the following from inside the bench directory:

```bash
cd ~/lms_backend/lms-bench        # staging path; adjust for production
source env/bin/activate

# 1. Fetch and install the app from GitHub
bench get-app git@github-yoisho:AshwiniUpadhy/yoisho_bunny.git

# 2. Install the app on the site
bench --site <your-site> install-app yoisho_bunny

# 3. Run migrations (syncs hooks, scheduled jobs, etc.)
bench --site <your-site> migrate

# 4. Restart the bench
bench restart
```

> **Note:** `bench restart` calls `supervisorctl restart frappe:` in production.
> If that fails, restart via your process manager or `sudo systemctl restart <service>`.

### Local dev install (from local git repo, no server needed)

```bash
cd /path/to/lms-bench
source env/bin/activate

bench get-app file:///home/tr-ashwini/Desktop/Ashwini_projects/yoisho_bunny
bench --site lms_site.com install-app yoisho_bunny
bench --site lms_site.com migrate
# Restart honcho (Ctrl-C and re-run) or: bench restart
```

### Uninstall procedure (if needed)

```bash
bench --site <your-site> uninstall-app yoisho_bunny --yes
bench remove-app yoisho_bunny
```

Frappe archives the removed app to `archived/apps/yoisho_bunny-<date>` automatically.

### Update the app (after pushing new commits to GitHub)

```bash
cd apps/yoisho_bunny
git pull
cd ../..
bench --site <your-site> migrate
bench restart
```

---

## Step 3 — Add Bunny.net credentials to site_config.json

Open the site config file:

```
sites/<your-site>/site_config.json
```

Add the following keys alongside the existing DB config:

```json
{
  "bunny_enabled": 0,
  "bunny_region": "",
  "bunny_public_zone": "<storage-zone-name-public>",
  "bunny_public_password": "<storage-zone-read-write-password-public>",
  "bunny_public_host": "https://<your-public-pullzone>.b-cdn.net",
  "bunny_public_pullzone_id": 0,
  "bunny_private_zone": "<storage-zone-name-private>",
  "bunny_private_password": "<storage-zone-read-write-password-private>",
  "bunny_private_host": "https://<your-private-pullzone>.b-cdn.net",
  "bunny_private_token_key": "<token-authentication-key-from-pull-zone-security-tab>",
  "bunny_public_folders": ["files/"],
  "bunny_api_key": "<bunny-account-api-key>"
}
```

**Where to find each value in the Bunny.net dashboard:**

| Key                        | Where to find it                                                                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bunny_region`             | Leave `""` for Frankfurt (default). Use `"ny"`, `"sg"`, `"uk"`, `"la"` etc. if your zone is in a different region — check Storage Zone → FTP & API Access hostname |
| `bunny_public_zone`        | Storage Zone name (e.g. `yoisho-public`)                                                                                                                           |
| `bunny_public_password`    | Storage Zone → FTP & API Access → **Password** (read/write, not read-only)                                                                                         |
| `bunny_public_host`        | Pull Zone → General → **Hostname** (e.g. `https://yoisho-public.b-cdn.net`)                                                                                        |
| `bunny_public_pullzone_id` | Pull Zone numeric ID — visible in the URL when editing the Pull Zone                                                                                               |
| `bunny_private_zone`       | Storage Zone name for private files                                                                                                                                |
| `bunny_private_password`   | Private Storage Zone → FTP & API Access → **Password**                                                                                                             |
| `bunny_private_host`       | Private Pull Zone hostname                                                                                                                                         |
| `bunny_private_token_key`  | Private Pull Zone → **Security** tab → Token Authentication → secret key (enable Token Authentication first)                                                       |
| `bunny_api_key`            | Bunny.net Account → **API** section → Account API Key                                                                                                              |

> **Keep `bunny_enabled: 0` until Step 5 is complete.**

After editing, restart the bench to reload the config:

```bash
bench restart
```

### Enable Server Scripts (required for day-to-day operation)

Add this to `sites/common_site_config.json` (bench-wide, NOT the site-specific `site_config.json`):

```json
"server_script_enabled": true
```

Then restart the bench. Without this, Server Scripts created in Frappe Desk will throw `ServerScriptNotEnabled`.

---

## Step 4 — Verify configuration

From the bench, confirm both zones are configured correctly:

```bash
bench --site <your-site> execute yoisho_bunny.bunny.status
```

Expected output should show:

```json
{
  "enabled": false,
  "public_configured": true,
  "private_configured": true,
  ...
}
```

If either `public_configured` or `private_configured` is `false`, recheck the credentials in `site_config.json`.

---

## Step 5 — Backfill existing files to Bunny

Before enabling live offload, push all existing files to Bunny.

**5a. Dry run first (no actual uploads, just a report):**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': True}"
```

**5b. Live backfill (enqueued as a background job):**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.backfill_start --kwargs "{'dry_run': 0}"
```

Monitor progress in **Frappe Desk → Background Jobs**. Large sites may take 10–30 minutes.

**5c. Verify a sample of files landed on Bunny:**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.verify_sample --kwargs "{'n': 100}"
```

Zero missing = backfill complete.

---

## Step 6 — Rewrite legacy absolute URLs (if applicable)

If course lesson content or other fields contain hardcoded absolute URLs like
`https://edu.yoisho.in/files/...`, strip them to relative paths:

**Dry run:**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': True}"
```

**Live:**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.rewrite_absolute_urls_start --kwargs "{'dry_run': 0}"
```

Skip this step if the output reports 0 fields affected.

---

## Step 7 — Enable the integration

Once the backfill and verify_sample pass with zero missing files, enable live offload:

In `site_config.json`, change:

```json
"bunny_enabled": 1
```

Then restart the bench. From this point:

- Every new file upload is automatically offloaded to Bunny after being saved locally
- Files deleted in Frappe are also deleted from Bunny
- A public→private privacy flip removes the file from the public zone immediately
- A daily reconciliation sweep runs as a backstop

---

## Step 8 — Point the frontend at the CDN

In the frontend deployment, set the environment variable:

```
VITE_BUNNY_CDN_URL=https://<your-public-pullzone>.b-cdn.net
```

Rebuild and redeploy the frontend. The media resolver will now serve public files
from the CDN instead of the Frappe origin. Private files are served via signed URLs
through `get_signed_media_url`.

---

## Step 9 (Phase 1.5, optional) — Reclaim local disk space

After running on the CDN for a soak period (recommended: 1–2 weeks with zero 404s),
delete the local originals that are confirmed on Bunny to free up disk.

**Dry run (lists what would be deleted):**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.reclaim_local --kwargs "{'dry_run': True}"
```

**Live (enqueued as a background job):**

```bash
bench --site <your-site> execute yoisho_bunny.bunny.reclaim_local_start --kwargs "{'dry_run': 0}"
```

> **Only run this after verify_sample reports 0 missing files.**
> The function HEAD-checks every file on Bunny before deleting — it will never
> delete a file that isn't confirmed on Bunny.

---

## Day-to-day operation (no SSH needed)

After installation, all operations can be triggered from **Frappe Desk → Server Scripts**
using `frappe.call()` or `frappe.enqueue()`. All methods require System Manager role.

| Method                                                    | What it does                                                 |
| --------------------------------------------------------- | ------------------------------------------------------------ |
| `yoisho_bunny.bunny.status`                               | Check current config and zone status                         |
| `yoisho_bunny.bunny.enable`                               | Turn offload ON                                              |
| `yoisho_bunny.bunny.disable`                              | Kill-switch — stops all offloads, existing CDN keeps serving |
| `yoisho_bunny.bunny.set_setting(name, value)`             | Change a non-secret setting at runtime                       |
| `yoisho_bunny.bunny.offload_one(file_name, force)`        | Manually offload / retry a single file                       |
| `yoisho_bunny.bunny.check_file(file_url)`                 | Check if a file exists on Bunny                              |
| `yoisho_bunny.bunny.list_bunny_files(kind, path_prefix)`  | List files in a storage zone                                 |
| `yoisho_bunny.bunny.delete_one(file_url, kind)`           | Delete a single object from Bunny                            |
| `yoisho_bunny.bunny.purge_cdn_url(file_url)`              | Purge one URL from the CDN cache                             |
| `yoisho_bunny.bunny.verify_sample(n)`                     | HEAD-check n recent files on Bunny                           |
| `yoisho_bunny.bunny.reconcile_now`                        | Run reconciliation sweep immediately                         |
| `yoisho_bunny.bunny.backfill_start(dry_run, limit)`       | Enqueue backfill of existing files                           |
| `yoisho_bunny.bunny.rewrite_absolute_urls_start(dry_run)` | Enqueue legacy URL rewrite                                   |
| `yoisho_bunny.bunny.reclaim_local_start(dry_run, limit)`  | Enqueue Phase 1.5 disk cleanup                               |
| `yoisho_bunny.bunny.get_signed_media_url(file_url, ttl)`  | Get signed URL for private file                              |
| `yoisho_bunny.bunny.run_purge(dry_run, confirm_token)`    | Phase 2 — purge old batch media                              |

**Example Server Script (API type):**

```python
# frappe is pre-injected — do NOT write `import frappe`
# Use frappe.call() to invoke any @frappe.whitelist() method from your apps
result = frappe.call("yoisho_bunny.bunny.status")
frappe.response["message"] = result
```

**Example: trigger a background reclaim job**

```python
dry_run = int(frappe.form_dict.get("dry_run", 1))
limit = frappe.form_dict.get("limit") or None

result = frappe.call("yoisho_bunny.bunny.reclaim_local_start", dry_run=dry_run, limit=limit)
frappe.response["message"] = result
```

---

## Tunable settings (no redeploy needed)

These can be changed at runtime via `set_setting` or directly in `site_config.json`
followed by a bench restart:

| Setting name        | Description                                            |
| ------------------- | ------------------------------------------------------ |
| `enabled`           | Master kill-switch (`1`/`0`)                           |
| `offload_enabled`   | Pause new-upload offload without disabling signed URLs |
| `reconcile_enabled` | Enable/disable the daily reconciliation sweep          |
| `signed_url_ttl`    | Signed URL lifetime in seconds (60–3600, default 300)  |
| `public_host`       | CDN hostname for public files                          |
| `private_host`      | CDN hostname for private files                         |
| `public_zone`       | Public storage zone name                               |
| `private_zone`      | Private storage zone name                              |
| `public_folders`    | List of path prefixes routed to the public zone        |
| `region`            | Storage region prefix (empty = Frankfurt)              |

> **Secrets** (`bunny_public_password`, `bunny_private_password`,
> `bunny_private_token_key`, `bunny_api_key`) can only be changed in
> `site_config.json` — they are intentionally blocked from `set_setting`.

---

## Troubleshooting

**Uploads not going to Bunny:**

- Run `status` and confirm `enabled: true` and `public_configured: true`
- Check **Frappe Desk → Error Log** filtered by title `yoisho_bunny`

**Files showing as missing in verify_sample:**

- Rerun `backfill_start` with `dry_run: 0` — it skips already-uploaded files (idempotent)

**Private file 401 errors:**

- Confirm `bunny_private_token_key` matches the key in Pull Zone → Security → Token Authentication
- Token Authentication must be **enabled** on the private Pull Zone

**Need to disable urgently:**

- Run `disable` from a Server Script or set `bunny_enabled: 0` in `site_config.json` + restart
- All existing Bunny objects keep serving; new uploads stay local
