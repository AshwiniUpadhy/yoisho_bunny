# Bunny.net Media Migration — Backend Handoff

**Audience:** backend developer implementing/owning the Frappe app.
**First target:** staging `yoisho-lms.theradixlab.com` (do NOT touch prod `edu.yoisho.in` until staging is signed off).
**Status:** frontend plumbing is shipped (inert); the Frappe app `yoisho_bunny` is scaffolded in this folder and compiles. Your job is to provision Bunny, deploy the app on the bench, run the backfill, and validate on staging.

---

## Introduction — what we're doing and why

**Today**, every piece of media on the LMS — course images, lesson images/audio/video, user avatars, blog images, and private KYC documents (PAN cards, ID/address proofs) — is stored on the **Frappe server's local disk** and served directly from the Frappe origin (`edu.yoisho.in` in prod, `yoisho-lms.theradixlab.com` on staging). That has three problems: it's **slow** for users far from the server, it puts **bandwidth/load** on the application server, and it **occasionally 404s** (stale deploys, permission edge cases, non-ASCII filename encoding).

**What we're building:** move that media onto **Bunny.net** — a combination of **Edge Storage** (global object storage) and a **CDN** (a network of edge caches worldwide). Files are stored once and served from the edge location nearest each user, so media loads **fast and reliably with zero 404s**. Crucially, the way files get uploaded does **not** change: Frappe still handles the upload, and our app transparently copies each file to Bunny behind the scenes.

Media splits into two categories, handled differently for security:
- **Public media** (course/lesson content, avatars, blog images) → a **public** Bunny CDN, no authentication.
- **Private media** (KYC/PAN documents) → a **separate, access-controlled** Bunny CDN, served only via **short-lived signed URLs** minted by Frappe after a permission check. These must never be exposed publicly.

**The end goal has two parts:**
1. **Phase 1 (this handoff):** fast, reliable serving from Bunny with zero 404s.
2. **Later phases:** **free up Frappe disk** by removing the local copies (Phase 1.5), and an **automated cleanup** that deletes media belonging to old, unpublished course batches from Bunny (Phase 2).

We are doing this **staging-first** on `yoisho-lms.theradixlab.com`. Prod is untouched until staging is signed off.

## How it works (runtime data flow)

Once deployed, four flows are in play. Note that **the file record's URL stays relative** (`/files/<name>`) in Frappe — the frontend simply swaps its base host to the CDN. That's what makes cutover and rollback a single config change.

```text
UPLOAD  (unchanged endpoint; offload is transparent)
  Browser ──POST /method/upload_file──▶ Frappe  (writes local /files/x, creates File record)
                                          │  after_insert hook (this app)
                                          ▼
                                   Bunny Storage  →  public zone  OR  private zone
                                          (upload is checksum-verified)

PUBLIC READ
  Browser ──GET https://<cdn-host>/files/x──▶ Bunny CDN ──cache miss──▶ Bunny Storage
            (frontend base host = VITE_BUNNY_CDN_URL)

PRIVATE READ  (permission-checked, no public exposure)
  Browser ──get_signed_media_url(/private/files/x)──▶ Frappe  (checks permission)
  Frappe  ──returns short-lived signed URL──▶ Browser
  Browser ──GET signed URL──▶ Bunny private CDN  (validates token + expiry)

DELETE
  Frappe deletes a File  ──on_trash hook──▶ delete the matching object on Bunny
```

Safety built into the flow: the offload hook **never fails an upload** (on any Bunny error it logs and leaves the local copy, which still serves); a private file can **never** be written to the public zone (asserted in code + a daily reconcile sweep); and local originals are **kept as a fallback** throughout Phase 1.

## The migration workflow — end-to-end steps

This is the whole rollout, in order. Steps 1–7 are Phase 1 (this handoff), run on staging first, then repeated on prod after sign-off. Steps 8–9 are later phases, explicitly **not** now.

1. **Provision Bunny** — create the two storage zones + two pull zones, enable token auth on the private one (§5).
2. **Configure + install** — put keys in `site_config.json` (§6), install the app on the bench (§10). From here, **new** uploads auto-offload to Bunny.
3. **Backfill existing files** — dry-run → live upload of everything already on disk, then `verify_sample` must report **0 missing** (§9). Local originals are never touched.
4. **Rewrite legacy absolute URLs** — relative-ize any old absolute `/files/` URLs baked into content so they resolve through the CDN (§9, step 2).
5. **Cutover (public)** — flip the frontend `VITE_BUNNY_CDN_URL` to the CDN host and redeploy the SPA. Public media now serves from Bunny (§8).
6. **Rewire private media** — point the frontend's private-file fetches at `get_signed_media_url` (§8).
7. **Soak** — 1–2 weeks with local originals retained and the `img onError → origin` fallback active; monitor the Error Log. **→ staging sign-off gate, then repeat 1–7 on prod.**
8. **(Phase 1.5 — later)** Add the Frappe download-override, then prune local originals to reclaim disk.
9. **(Phase 2 — later)** Add the `LMS Batch.media_frozen_on` field and enable the scheduled purge (dry-run → soft-delete → hard-delete) of old unpublished-batch media.

### Phases at a glance

| Phase | Scope | Ship gate |
|-------|-------|-----------|
| **1** (this handoff) | Offload + backfill + serve public via CDN, private via signed URLs | Staging test plan (§10) passes → then prod |
| **1.5** (later) | Reclaim Frappe disk: download-override + prune local originals | After a 1–2 week Phase-1 soak |
| **2** (later) | Scheduled purge of Bunny media for old, non-published batches | After Phase 1.5 + the purge reference index is validated |

---

## 1. Goal

Serve all Frappe LMS media from **Bunny.net** (fast, reliable, **zero 404s**) instead of the Frappe origin, and eventually **free Frappe disk** by pruning local originals. A later phase (2) purges Bunny media of old non-published batches. This handoff covers **Phase 1** (offload + serve) on staging.

## 2. Architecture (decisions already locked — please don't re-litigate)

- **True Edge-Storage offload.** Bunny is the authoritative store. `File.file_url` in Frappe **stays relative** (`/files/<name>`), and the **frontend swaps the base host**. So the DB never stores absolute CDN URLs, and cutover/rollback is a single frontend env var.
- **Native HTTP Storage API**, NOT the S3-compatible API (it was closed-preview in 2026 — do not depend on it). Endpoint `https://{region}storage.bunnycdn.com/{zone}/{path}`, header `AccessKey`.
- **Two storage zones** (physical isolation): a **public** zone (no token) for `/files/…`, and a **private, token-authenticated** zone for KYC/PAN (`/private/files/…`) served via server-minted HMAC-signed URLs.
- **Real Frappe app, not a Server Script.** `safe_exec` (Server Scripts / System Console) cannot `import requests` or do binary HTTP, so all Bunny I/O must live in this installed app.
- **Reclaim disk** is the end goal, so two things become mandatory later (Phase 1.5, not now): a download-override so Frappe desk/print/email resolve bytes from Bunny after prune, and a legacy absolute-URL rewrite. Local originals are the fallback until then.

## 3. What's done vs. what you own

**Already done (frontend, in the SPA repo — deployed inert):**
- Shared resolver `resolvePublicMedia()` in `src/utils/mediaUrl.js`: public `/files/…` → CDN base, `/private/files/…` → Frappe, absolute/`data:`/`blob:` pass through.
- `src/utils/htmlUtils.js` repointed; `toRelativeUrls` strips both bases (reversibility).
- `resolveImage` (demoApi/webinarApi), `resolveAvatarUrl` (useUserAvatar), and 12 inline card-image sites use the shared resolver.
- New env var `VITE_BUNNY_CDN_URL` (currently **empty** = kill-switch → falls back to `VITE_LMS_BASE_URL`, so nothing changed yet).
- Left on Frappe intentionally: `certificateApi` printview, `CertificateDispatch` email links, `PurchasedProducts`/`OrderHistory` invoice downloads.

**You own (backend):**
- Provision Bunny (§5), configure the site (§6), review/complete + deploy this app (§7), run the backfill (§9), and validate on staging (§10). Then the frontend team flips `VITE_BUNNY_CDN_URL` and rewires private media (§8).

## 4. Frontend ↔ backend contract (must not break)

- **Uploads are unchanged.** The frontend keeps calling Frappe's standard `POST /method/upload_file` (via `src/api/fileApi.js`, `assignmentApi.js`) and depends on the response shape `{"message": {"file_url": "...", "file_name": "..."}}`. **Do not change the upload endpoint or its response.** Your offload happens *after* Frappe writes the File, transparently.
- **Public serving:** the frontend builds `https://<cdn-host>/files/<name>` from the relative `file_url`. So the Bunny object key **must** be exactly `files/<name>` (and `private/files/<name>` for private), byte-for-byte matching the path — including percent-encoding of non-ASCII filenames (see the `normalize_key` note in §7 / §11).
- **Private serving:** the frontend will call a new endpoint you expose:
  ```
  GET {VITE_API_BASE_URL}/method/yoisho_bunny.bunny.get_signed_media_url?file_url=/private/files/x.pdf
  (session cookie, credentials:'include') → returns a short-lived signed CDN URL string
  ```

## 5. Provision Bunny — STAGING

Create these in the Bunny dashboard for staging (keep them separate from prod):

| Zone | Purpose | Pull Zone hostname (staging suggestion) | Token auth |
|------|---------|------------------------------------------|-----------|
| `yoisho-public-staging`  | public `/files/…` | `yoisho-public-staging.b-cdn.net` (or a staging CNAME) | **off** |
| `yoisho-private-staging` | KYC/PAN `/private/files/…` | `yoisho-private-staging.b-cdn.net` | **on** (V2, SHA256, query token) |

Steps:
1. Create the two **storage zones** (Storage → Add Storage Zone). This screen only has name/tier/region/replication — **no auth options, that's expected**, storage zones aren't where Token Authentication lives. Note the primary **region code** (`""`=DE, else `ny`/`sg`/`uk`/`la`/`se`/`br`/`jh`/`syd`) and each zone's **password** (= Edge Storage `AccessKey`, under the zone's FTP & API Access tab).
2. **Public Pull Zone** (CDN → Add Pull Zone) → origin = `yoisho-public-staging` storage zone. Using the default `*.b-cdn.net` hostname on staging avoids CNAME/SSL setup. Perma-cache on.
3. **Private Pull Zone** (CDN → Add Pull Zone) → origin = `yoisho-private-staging`. Token Authentication lives HERE, not on the storage zone: open the Pull Zone → **Security** tab → toggle **Token Authentication** on → copy the **URL Token Authentication Key** it generates. Confirm the scheme is **SHA256, query-string token** (that's what `sign_private_url` produces — validate with a test URL, §11).
4. Capture the **public pull-zone ID** and an **account API key** (only needed for Phase 2 cache purge; optional now).

> Decision to confirm: staging CDN hostnames — raw `b-cdn.net` (fastest) vs a staging subdomain CNAME. Recommend `b-cdn.net` for staging.

## 6. Configure the staging site (`site_config.json`)

Secrets go in **`site_config.json` only** (never a DocType — the AccessKey must never be readable via `/api/resource`):

```json
{
  "bunny_enabled": 1,
  "bunny_region": "",
  "bunny_public_zone": "yoisho-public-staging",
  "bunny_public_password": "<public zone AccessKey>",
  "bunny_public_host": "https://yoisho-public-staging.b-cdn.net",
  "bunny_private_zone": "yoisho-private-staging",
  "bunny_private_password": "<private zone AccessKey>",
  "bunny_private_host": "https://yoisho-private-staging.b-cdn.net",
  "bunny_private_token_key": "<private pull-zone URL token security key>",
  "bunny_public_folders": ["files/"],
  "bunny_public_pullzone_id": 0,
  "bunny_api_key": ""
}
```

`bunny_enabled: 0` is the **kill-switch** (hooks no-op; existing objects still serve; new uploads stay local).

## 7. The app — one logic file

**All logic lives in a single module, `yoisho_bunny/bunny.py`** (so it can be pasted as one file — see §7a for install). The only other files are the standard Frappe app boilerplate:

```text
frappe_apps/yoisho_bunny/
  yoisho_bunny/
    bunny.py        <- ALL logic (config, client, offload, api, reconcile, backfill, purge, control plane)
    hooks.py        <- registers the File doc_events + daily reconcile (points at bunny.*)
    __init__.py     modules.txt   patches.txt
  pyproject.toml  README.md  BACKEND_HANDOFF.md
```

`bunny.py` is organized into labelled sections; review these before trusting it, and **validate the two flagged items in §11**:

| Section in `bunny.py` | Responsibility |
|------|----------------|
| 1. CONFIG | Reads settings; secrets from `site_config.json`, non-secret toggles from a runtime store (see §7b). Per-zone `is_configured()` gates. |
| 2. BUNNY CLIENT | Native Edge Storage HTTP (`put_object` w/ SHA256 `Checksum` verify, `head_object`, `delete_object`, `move_object`); `normalize_key` (encodeURI parity); `sign_private_url` (token auth). |
| 3. FILE OFFLOAD | `File` `after_insert`/`on_update`/`on_trash` → offload / reconcile / delete. **Never raises**; local copy stays. Public/private routing asserted in code. |
| 4. WHITELISTED API | `get_signed_media_url` — permission-checked signed URL for private media (replaces the client-side Administrator token fetch). |
| 5. RECONCILE | Daily backstop: delete any private file's bytes off the public zone. |
| 6. BACKFILL | One-time `backfill_run` / `rewrite_absolute_urls` / `verify_sample`. Idempotent, resumable; never touches local originals. |
| 7. PURGE | **Phase 2, unscheduled.** Object-level purge with full-corpus reference index + strict-subset test + soft-delete + dry-run gate. Do not enable now. |
| 8. CONTROL PLANE | Whitelisted `enable`/`disable`/`status`/`set_setting`/`backfill_start`/`reconcile_now` — operate the app **without SSH** (see §7b). |

### 7a. Minimal install (paste one file)

The boilerplate is generated for you — you don't hand-write it:

```bash
bench new-app yoisho_bunny        # generates hooks.py, __init__.py, modules.txt, setup, etc.
# then: paste our bunny.py into apps/yoisho_bunny/yoisho_bunny/bunny.py
#       and paste the doc_events + scheduler_events block from our hooks.py into the generated hooks.py
bench --site yoisho-lms.theradixlab.com install-app yoisho_bunny
bench --site yoisho-lms.theradixlab.com migrate && bench restart
```

(Or just `bench get-app` this whole `yoisho_bunny/` folder — it's a complete app.)

### 7b. Operating without server access (enable / disable / tune / trigger)

You do **not** need SSH for day-to-day operation. Non-secret settings live in a runtime store (Frappe's `DefaultValue`, seeded from `site_config.json`), and every operation is a whitelisted, System-Manager-only method. Three ways to drive it:

- **Desk** — a Client Script button: `frappe.call({ method: "yoisho_bunny.bunny.disable" })`.
- **REST** — `POST {site}/api/method/yoisho_bunny.bunny.disable` with `Authorization: token <key:secret>`.
- **Server Script** (no SSH) — flip the master switch directly: `frappe.db.set_default("bunny_enabled", "0")`.

| Need | How (no redeploy) |
|------|-------------------|
| **Kill-switch OFF/ON** | `disable()` / `enable()` — or Server Script `frappe.db.set_default("bunny_enabled","0")`. Existing objects keep serving; new uploads stay local. |
| **Pause only new-upload offload** | `set_setting("offload_enabled", 0)` (serving + signed URLs still work). |
| **Pause the reconcile sweep** | `set_setting("reconcile_enabled", 0)`. |
| **Retune host/zone/region/TTL/folders** | `set_setting("signed_url_ttl", 600)`, `set_setting("public_host", "https://…")`, etc. (allowed keys only; secrets refused). |
| **Trigger backfill / rewrite** | `backfill_start(dry_run=1)` / `rewrite_absolute_urls_start(dry_run=1)` (enqueued as background jobs). |
| **Trigger reconcile now** | `reconcile_now()`. |
| **Run phase-2 purge (later)** | `run_purge(dry_run=1)`. |
| **See current state** | `status()` (returns effective config, no secrets). |

**The honest boundary:** changing the *core Bunny I/O logic itself* (the byte upload/delete) still needs a `bunny.py` edit + `bench restart` — that code cannot run in a Server Script / System Console because `safe_exec` blocks `import requests` and binary HTTP. Everything operational above is deliberately config-driven so you rarely touch code. If a user reports a media error, first check `status()` and the Error Log; most fixes are a `set_setting` / `disable`, not a code change.

Key implementation guarantees already coded (please preserve):
- Handlers wrapped in try/except + `frappe.log_error`; they return silently on failure.
- Uploads are **checksum-verified by Bunny** (a 201 means stored bytes match) — no second round-trip needed.
- `file_url` is **never rewritten** to absolute.

## 8. Frontend wiring (happens AFTER staging backfill verifies)

1. **Public cutover:** set `VITE_BUNNY_CDN_URL=https://yoisho-public-staging.b-cdn.net` in the SPA's `.env.development` (staging build) and redeploy the staging frontend. (Empty until backfill is 100% verified.)
2. **Private media:** the frontend team repoints the current `Authorization: VITE_API_TOKEN` blob fetches (MediaAudio, demo/webinar private images, AffiliateDetailModal, fileApi private downloads) to `get_signed_media_url`. Confirm the endpoint returns a working signed URL first.

## 9. Backfill runbook (staging)

```bash
# 0. snapshot first
bench --site yoisho-lms.theradixlab.com backup
tar czf files-backup.tgz sites/yoisho-lms.theradixlab.com/public/files \
                          sites/yoisho-lms.theradixlab.com/private/files

# 1. upload (dry-run, then live). Idempotent + resumable; skips already-present.
bench --site yoisho-lms.theradixlab.com execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': True}"
bench --site yoisho-lms.theradixlab.com execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': False}"

# 2. relative-ize any legacy ABSOLUTE origin URLs baked into content (dry-run first)
bench --site yoisho-lms.theradixlab.com execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': True}"
bench --site yoisho-lms.theradixlab.com execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': False}"

# 3. verify a sample resolves on Bunny (expect: missing 0)
bench --site yoisho-lms.theradixlab.com execute yoisho_bunny.bunny.verify_sample --kwargs "{'n': 100}"
```

Local originals are never touched. Only after `verify_sample` reports **0 missing** does the frontend flip `VITE_BUNNY_CDN_URL`.

## 10. Staging test plan (sign-off gates)

Install:
```bash
bench get-app yoisho_bunny /path/to/frappe_apps/yoisho_bunny
bench --site yoisho-lms.theradixlab.com install-app yoisho_bunny
bench --site yoisho-lms.theradixlab.com migrate && bench restart
```

Then verify, in order:
1. **Live public upload:** upload a course image via the app UI → confirm the object appears in `yoisho-public-staging` under key `files/<name>` and `GET https://<public-host>/files/<name>` returns 200 with the right bytes.
2. **Private isolation:** upload a KYC doc (`is_private=1`) → confirm it lands in `yoisho-private-staging` and is **NOT** in the public zone; `GET` the public host for it → 404; `GET` the private host **without** a token → blocked.
3. **Signed URL:** call `get_signed_media_url` as a permitted user → returns a URL that loads; as a non-permitted user / Guest → PermissionError; let the token expire → URL stops working.
4. **Backfill:** run §9; `verify_sample` → **0 missing**; spot-check a filename with an apostrophe / non-ASCII char resolves on the CDN (the `normalize_key` correctness check).
5. **Cutover:** set staging `VITE_BUNNY_CDN_URL`, redeploy staging frontend → browse Store, course pages, lessons (EditorJS media), quizzes (audio), avatars → all load from the CDN host; check DevTools Network for any 404.
6. **Reconcile:** flip a public File to `is_private=1` → after the daily job (or run `yoisho_bunny.bunny.run_reconciliation` manually) confirm its object is removed from the public zone.
7. **Fallback:** temporarily set a bad `bunny_public_password` and upload → confirm `upload_file` still succeeds, an Error Log entry is written, and the file stays served from the Frappe origin.

## 11. Safety rules & landmines (do not violate)

- **Never raise from a File hook.** `/method/upload_file` must never fail because of Bunny.
- **Private media must never reach the public zone.** Routing is asserted in code + a daily reconcile; keep both. One leaked PAN scan on a geo-replicated edge is unrecoverable.
- **Object key must match the requested URL exactly**, including percent-encoding. `normalize_key` mirrors the frontend's `encodeURI(decodeURI())` (`src/api/fileApi.js` `normalizeFileUrl`). **Validate** that a non-ASCII filename PUT by the app is retrievable at the URL the browser builds — this is the classic curly-apostrophe 404.
- **Validate `sign_private_url` against the live private pull zone.** The token algorithm (SHA256, query token) must match the zone's Token Authentication settings exactly; test a signed URL before relying on it.
- **Phase-2 purge is object/URL-level only.** `server_scripts/deep_clone_course.py` copies media fields verbatim, so the Original course and every batch's Duplicate share the **same** `/files/<name>` bytes / File doc. Never delete by walking a batch's course tree — delete a key only when its full corpus reference count is a strict subset of the doomed batches' lineage (already implemented in `bunny.py` (PURGE section)). Do not enable purge until `_build_reference_index` is extended to quiz media and validated.
- **30-day timer uses `LMS Batch.media_frozen_on`**, a new Custom Field you'll add — never `modified` (it resets on drip toggles, enrollment sync, and timesheet auto-publish).

## 12. Explicitly NOT in this phase

- Origin disk prune (Phase 1.5) and its prerequisite download-override.
- Scheduling the Phase-2 purge (the PURGE section of `bunny.py` stays unscheduled) and the "Bunny Purge Log" DocType.
- Any prod (`edu.yoisho.in`) changes.

## 13. Rollback & kill-switch

- **Kill-switch:** `bunny_enabled: 0` in `site_config.json` → offload hooks no-op; existing objects keep serving.
- **Frontend rollback (pre-prune):** set `VITE_BUNNY_CDN_URL` empty and redeploy → media resolves from the Frappe origin again. No DB migration (file_url stayed relative).

## 14. Open items to confirm with the team

- Staging CDN hostnames (b-cdn.net vs staging CNAME).
- Which `video_url` / `recorded_video_url` Data fields (if any) point at Frappe `/files/` uploaded mp4s (in scope) vs external embeds (out of scope) — resolve during the §9 Step-0 inventory before any prune.
- Signed-URL TTL for private media (default 300s; balance security vs re-mint frequency).
- What action sets `media_frozen_on` (publish-off timestamp vs a manual "freeze").
