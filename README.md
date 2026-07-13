# yoisho_bunny

Offloads Frappe LMS **File** storage to **Bunny.net Edge Storage** (native HTTP
API) fronted by a **Bunny CDN Pull Zone**, so media loads fast/reliably with zero
404s. Public course/lesson media goes to a public zone; private KYC/PAN goes to a
token‑authenticated private zone served via server‑minted signed URLs.

This app is **staged in the frontend repo for review/versioning**; it must be
deployed on the Frappe **bench** (it cannot run as a sandboxed Server Script —
`safe_exec` blocks `import requests` / binary HTTP).

> Phase 1 = offload + backfill + serving. Phase 1.5 = origin disk reclamation
> (gated). Phase 2 = scheduled purge (PURGE section of `bunny.py`, **not** scheduled yet). See the
> plan file `task-move-frappe-lms-goofy-cloud.md`.

---

## 1. Provision Bunny (do this first, per environment)

Two **storage zones** (physical isolation of private data):

| Zone | Purpose | Pull Zone | Token auth |
|------|---------|-----------|------------|
| `yoisho-public`  | public `/files/…` media | `cdn.yoisho.in` | **off** |
| `yoisho-private` | KYC/PAN `/private/files/…` | `private-cdn.yoisho.in` | **on** (V2, SHA256) |

Steps:
1. **Storage zones** → pick a primary region (note the region code: `""`=DE,
   `ny`, `sg`, `uk`, `la`, `se`, `br`, `jh`, `syd`). Copy each zone's **password**
   (this is the Edge Storage `AccessKey`).
2. **Public Pull Zone** → origin = the `yoisho-public` storage zone; hostname
   `cdn.yoisho.in` (add the CNAME + enable Bunny SSL). Turn Perma‑Cache on.
3. **Private Pull Zone** → origin = `yoisho-private`; hostname
   `private-cdn.yoisho.in`; **enable Token Authentication** and set a URL Token
   Security Key. Confirm the scheme is **SHA256, query‑string token** to match
   `sign_private_url` (validate with a signed test URL before relying
   on it — this is the one piece that must be checked against the live zone).
4. Capture the **public pull‑zone ID** and an **account API key** (for phase‑2
   cache purge).

## 2. Configure the site

Put secrets in **`site_config.json` only** (never a DocType):

```json
{
  "bunny_enabled": 1,
  "bunny_region": "",
  "bunny_public_zone": "yoisho-public",
  "bunny_public_password": "…",
  "bunny_public_host": "https://cdn.yoisho.in",
  "bunny_private_zone": "yoisho-private",
  "bunny_private_password": "…",
  "bunny_private_host": "https://private-cdn.yoisho.in",
  "bunny_private_token_key": "…",
  "bunny_public_folders": ["files/"],
  "bunny_public_pullzone_id": 123456,
  "bunny_api_key": "…"
}
```

`bunny_enabled: 0` is the **kill‑switch** — offload hooks no‑op, existing objects
keep serving, new uploads stay local.

## 3. Install

```bash
bench get-app yoisho_bunny /path/to/frappe_apps/yoisho_bunny   # or symlink/copy into apps/
bench --site <site> install-app yoisho_bunny
bench --site <site> migrate
bench restart
```

Custom Field for phase 2 (add now, backfill later): **`LMS Batch.media_frozen_on`**
(Datetime). Set it when a batch is unpublished/frozen. The purge refuses to run
without it (never uses `modified`).

## 4. Backfill existing files (zero‑404 order of operations)

```bash
# 0. snapshot first
bench --site <site> backup
tar czf files-backup.tgz sites/<site>/public/files sites/<site>/private/files

# 1. dry-run, then live upload (idempotent + resumable; skips already-present)
bench --site <site> execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': True}"
bench --site <site> execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': False}"

# 2. relative-ize any legacy ABSOLUTE origin URLs baked into content (dry-run first)
bench --site <site> execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': True}"
bench --site <site> execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': False}"

# 3. verify a sample resolves on Bunny
bench --site <site> execute yoisho_bunny.bunny.verify_sample --kwargs "{'n': 100}"
```

Local originals are **never** touched by backfill. Only after `verify_sample`
reports **0 missing** do you flip the frontend `VITE_BUNNY_CDN_URL` (atomic cutover).

## 5. Frontend wiring (in the SPA repo)

- Public media: already routed through `resolvePublicMedia()`; set
  `VITE_BUNNY_CDN_URL=https://cdn.yoisho.in` **after** backfill verifies.
- Private media: repoint the current `Authorization: VITE_API_TOKEN` blob fetches
  (MediaAudio, demo/webinar private images, AffiliateDetailModal, fileApi private
  downloads) to call this app's session‑auth endpoint and use the returned URL:

  ```
  GET {VITE_API_BASE_URL}/method/yoisho_bunny.bunny.get_signed_media_url?file_url=/private/files/x.pdf
  # send session cookie (credentials: 'include'); returns a short-lived signed URL
  ```

---

## Layout

All logic is in **one file, `yoisho_bunny/bunny.py`** (paste-friendly). The rest is
standard Frappe app boilerplate (`hooks.py`, `__init__.py`, `modules.txt`, install
metadata). `bunny.py` sections:

| Section | Role |
|---------|------|
| CONFIG | settings (secrets from `site_config.json`; non‑secret toggles from a runtime store); per‑zone `is_configured()` |
| BUNNY CLIENT | native Edge Storage HTTP (PUT/HEAD/DELETE/move), `normalize_key` (encodeURI parity), `sign_private_url` (token auth) |
| FILE OFFLOAD | `File` `after_insert`/`on_update`/`on_trash` → offload / reconcile / delete; never raises; public↔private routing asserted in code |
| WHITELISTED API | `get_signed_media_url` (permission‑checked signed URL for private media) |
| RECONCILE | daily backstop: strip any private file's bytes off the public zone |
| BACKFILL | one‑time `backfill_run` / `rewrite_absolute_urls` / `verify_sample` |
| PURGE | **phase 2** (unscheduled): object‑level purge with full‑corpus ref index + soft‑delete + dry‑run gate |
| CONTROL PLANE | whitelisted `enable`/`disable`/`status`/`set_setting`/`backfill_start`/`reconcile_now` — run without SSH |

## Operate without SSH

Non‑secret settings live in a runtime store (Frappe `DefaultValue`, seeded from
`site_config.json`); every op is a whitelisted, System‑Manager‑only method. Drive it from
a Desk button (`frappe.call({method:"yoisho_bunny.bunny.disable"})`), REST
(`POST /api/method/yoisho_bunny.bunny.disable`), or a Server Script
(`frappe.db.set_default("bunny_enabled","0")`).

- Kill‑switch: `disable()` / `enable()`  · pause offload only: `set_setting("offload_enabled",0)`
- Retune: `set_setting("signed_url_ttl",600)`, `set_setting("public_host","https://…")` (secrets refused)
- Trigger: `backfill_start(dry_run=1)`, `rewrite_absolute_urls_start(dry_run=1)`, `reconcile_now()`, `run_purge(dry_run=1)` · inspect: `status()`

**Boundary:** changing the *core Bunny I/O logic* still needs a `bunny.py` edit + `bench restart`
(binary HTTP can't run in `safe_exec`). Everything else above is config, no redeploy.

## Behavior & safety notes

- **Never raises** from a File hook → `/method/upload_file` can't fail on Bunny.
- **file_url stays relative** → cutover/rollback is one frontend env var.
- **Integrity**: PUT sends a SHA256 `Checksum`; Bunny 400s on mismatch, so a 201 is
  proof the stored bytes match (`put_object`).
- **Private never on public zone**: routing asserted in code + daily reconcile.
- **Purge safety**: deletes a key only when its full corpus reference count is a
  strict subset of the doomed batches' lineage (guards against `deep_clone_course`
  sharing byte‑identical media across Original + sibling batches); soft‑deletes to
  `trash/`; dry‑run manifest + confirm‑token before any destructive run.

## Not yet done (tracked in the plan)

- Download override so Frappe desk/print/email resolve bytes from Bunny **after**
  disk reclamation (Phase 1.5) — required before pruning local originals.
- "Bunny Purge Log" DocType to persist dry‑run manifests for human sign‑off.
- Extend `_build_reference_index` to quiz media and any new media fields
  before enabling the scheduled purge.
- Validate `sign_private_url` against the live private pull zone's token settings.
