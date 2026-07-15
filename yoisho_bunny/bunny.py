"""yoisho_bunny — single-file implementation.

All logic for offloading Frappe LMS File storage to Bunny.net Edge Storage + CDN
lives in this ONE module so it can be pasted onto the server as a single file.
hooks.py points every entrypoint here (yoisho_bunny.bunny.<fn>).

Sections:
  1. CONFIG          — read site_config.json (secrets never in a DocType)
  2. BUNNY CLIENT    — native Edge Storage HTTP API + CDN token signing
  3. FILE OFFLOAD    — File doc_event handlers (after_insert / on_update / on_trash)
  4. WHITELISTED API — get_signed_media_url (frontend, session-authed)
  5. RECONCILE       — daily backstop: keep private bytes off the public zone
  6. BACKFILL        — one-time bench-run migration of existing files
  7. PURGE           — PHASE 2, unscheduled: delete old unpublished-batch media

Golden rules (do not break):
  * Never raise from a File hook — /method/upload_file must never fail on Bunny.
  * Private (is_private=1 / /private/files/) media must NEVER touch the public zone.
  * file_url stays RELATIVE; the frontend swaps the base host. Local originals are
    deleted immediately after a confirmed Bunny upload (2xx + checksum match).
"""

import base64
import hashlib
import mimetypes
import re
import time
from urllib.parse import quote, unquote

import requests

import frappe

_LOG_TITLE = "yoisho_bunny"


def _log(message):
    frappe.log_error(title=_LOG_TITLE, message=message)


# =========================================================================== #
# 1. CONFIG
#
# ALL secrets live in site_config.json (read via frappe.conf) — NEVER a DocType,
# so the storage AccessKey can never be read back through /api/resource.
#
# Required keys in site_config.json:
#   "bunny_enabled": 1,
#   "bunny_region": "",                  # "" = DE, else "ny"/"sg"/"uk"/"la"/"se"/"br"/"jh"/"syd"
#   "bunny_public_zone": "yoisho-public",
#   "bunny_public_password": "<edge-storage-accesskey-public>",
#   "bunny_public_host": "https://cdn.yoisho.in",
#   "bunny_private_zone": "yoisho-private",
#   "bunny_private_password": "<edge-storage-accesskey-private>",
#   "bunny_private_host": "https://private-cdn.yoisho.in",
#   "bunny_private_token_key": "<pull-zone-url-token-security-key>",
#   "bunny_public_folders": ["files/"],
#   "bunny_public_pullzone_id": 123456,  # phase-2 cache purge (optional in v1)
#   "bunny_api_key": "<account-api-key>" # phase-2 cache purge (optional in v1)
# =========================================================================== #
DEFAULT_PUBLIC_FOLDERS = ["files/"]
_DEFAULT_TTL = 300
_MAX_TTL = 3600

# NON-SECRET settings that may be changed at runtime WITHOUT a redeploy — from
# Desk, a Server Script, or the whitelisted set_setting() endpoint. Secrets
# (zone passwords, the private token key) are read from site_config.json ONLY
# and can never be set through the API / a Server Script.
_TUNABLE = {
    "enabled", "offload_enabled", "reconcile_enabled",
    "region", "public_host", "private_host",
    "public_zone", "private_zone", "public_folders", "signed_url_ttl",
    "delete_on_upload", "local_patterns",
}


def _setting(name):
    """Effective value of a NON-SECRET setting.

    Priority: a runtime override in the DefaultValue store (editable without SSH
    via Desk / Server Script / set_setting) → then site_config.json. This is what
    lets you disable or retune the integration without touching the server.
    """
    val = frappe.db.get_default(f"bunny_{name}")
    if val in (None, ""):
        val = frappe.conf.get(f"bunny_{name}")
    return val


def _truthy(v):
    return str(v).strip().lower() not in ("", "0", "false", "none", "no")


def _flag(name):
    """A feature flag that defaults ON when unset, but is still gated by the master switch."""
    v = _setting(name)
    return enabled() and (v in (None, "") or _truthy(v))


def enabled():
    """Master kill-switch. Flip with disable()/enable() — no redeploy needed."""
    return _truthy(_setting("enabled"))


def offload_enabled():
    # Pause NEW-upload offload without disabling serving / signed URLs.
    return _flag("offload_enabled")


def delete_on_upload():
    """Delete local file immediately after a confirmed Bunny upload.

    OFF by default — keeps the local copy so Frappe can still serve requests
    directly. Enable on production once the CDN fallback patch is confirmed working.
    Set via: bench --site <site> set-config bunny_delete_on_upload 1
    """
    v = _setting("delete_on_upload")
    return enabled() and _truthy(v) if v not in (None, "") else False


def reconcile_enabled():
    return _flag("reconcile_enabled")


def region():
    # "" means the default DE region (storage.bunnycdn.com).
    return str(_setting("region") or "").strip()


def zone(kind):
    return _setting(f"{kind}_zone")


def password(kind):
    # SECRET — site_config.json only.
    return frappe.conf.get(f"bunny_{kind}_password")


def public_host():
    return str(_setting("public_host") or "").rstrip("/")


def private_host():
    return str(_setting("private_host") or "").rstrip("/")


def private_token_key():
    # SECRET — site_config.json only.
    return frappe.conf.get("bunny_private_token_key")


def signed_url_ttl():
    try:
        return max(60, min(int(_setting("signed_url_ttl") or _DEFAULT_TTL), _MAX_TTL))
    except (TypeError, ValueError):
        return _DEFAULT_TTL


def public_folders():
    val = _setting("public_folders")
    if isinstance(val, str) and val.strip():
        # accept a comma- or JSON-list string when set via set_setting()
        val = [p.strip() for p in val.strip("[] ").replace('"', "").split(",") if p.strip()]
    return val if isinstance(val, list) and val else DEFAULT_PUBLIC_FOLDERS


def local_patterns():
    """File URL prefixes that must stay on Frappe and are never offloaded to Bunny.

    Use for logos, favicons, and any file that must be served directly by Frappe.
    Configure in site_config.json:
        "bunny_local_patterns": ["/files/logo", "/files/favicon", "/files/brand/"]
    Files whose file_url starts with any of these prefixes are skipped by
    offload(), backfill, and verify_sample.
    """
    val = _setting("local_patterns")
    if isinstance(val, str) and val.strip():
        val = [p.strip() for p in val.strip("[] ").replace('"', "").split(",") if p.strip()]
    return list(val) if isinstance(val, list) else []


def _is_local_only(file_url):
    """True if file_url matches a local_patterns prefix — must stay on Frappe."""
    patterns = local_patterns()
    return bool(patterns and any(file_url.startswith(p) for p in patterns))


def is_configured(kind):
    """True only when the zone we're about to write to is fully configured."""
    if not (zone(kind) and password(kind)):
        return False
    if kind == "private" and not (private_host() and private_token_key()):
        return False
    if kind == "public" and not public_host():
        return False
    return True


# =========================================================================== #
# 2. BUNNY CLIENT — native Edge Storage HTTP API + CDN token signing
#
# We deliberately use the native HTTP API (NOT the S3-compatible API, which was
# still closed-preview as of 2026). Storage endpoint:
#   https://{region-prefix}storage.bunnycdn.com/{zone}/{path}   (AccessKey header)
# Upload integrity: we send SHA256 of the body in the `Checksum` header; Bunny
# validates it and returns 400 on mismatch, so a 2xx is proof the stored bytes
# match — no second round-trip to verify.
# =========================================================================== #

# Chars encodeURI() leaves un-escaped that matter in a file path. Keeping this in
# lock-step with the frontend's encodeURI(decodeURI()) (src/api/fileApi.js
# normalizeFileUrl) guarantees the object key we PUT matches the URL the browser
# later requests — the curly-apostrophe / non-ASCII 404 class.
_ENCODE_URI_SAFE = "/-_.!~*'();:@&=+$,?#"

_TIMEOUT = (5, 30)  # (connect, read) seconds


def normalize_key(file_url):
    """/files/Ann's note.pdf  ->  files/Ann's%20note.pdf  (matches the browser)."""
    path = (file_url or "").lstrip("/")
    try:
        path = unquote(path)
    except Exception:
        pass
    return quote(path, safe=_ENCODE_URI_SAFE)


def _storage_host():
    r = region()
    return f"{r}.storage.bunnycdn.com" if r else "storage.bunnycdn.com"


def _storage_url(kind, key):
    return f"https://{_storage_host()}/{zone(kind)}/{key}"


def _headers(kind, extra=None):
    h = {"AccessKey": password(kind), "accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def sha256_hex_upper(content_bytes):
    return hashlib.sha256(content_bytes).hexdigest().upper()


def put_object(kind, key, content_bytes, content_type="application/octet-stream"):
    """Upload bytes. Returns True on a verified (checksum-matched) 2xx."""
    checksum = sha256_hex_upper(content_bytes)
    headers = _headers(kind, {"Content-Type": content_type, "Checksum": checksum})
    resp = requests.put(_storage_url(kind, key), data=content_bytes, headers=headers, timeout=_TIMEOUT)
    return resp.status_code in (200, 201)


def head_object(kind, key):
    """Return (exists: bool, size: int|None).

    Bunny Edge Storage returns 401 on HEAD requests — use streaming GET instead,
    reading only the response headers and closing the connection without downloading
    the body.
    """
    with requests.get(
        _storage_url(kind, key), headers=_headers(kind), timeout=_TIMEOUT, stream=True
    ) as resp:
        if resp.status_code == 200:
            try:
                return True, int(resp.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return True, None
        return False, None


def delete_object(kind, key):
    """Delete an object (recursive for a directory key). Returns True on 2xx/404."""
    resp = requests.delete(_storage_url(kind, key), headers=_headers(kind), timeout=_TIMEOUT)
    # 404 is fine — the goal state (absent) is already met; idempotent.
    return resp.status_code in (200, 201, 204, 404)


def move_object(kind, src_key, dst_key):
    """Copy src -> dst then delete src (Bunny has no native move). Best-effort."""
    resp = requests.get(_storage_url(kind, src_key), headers=_headers(kind), timeout=_TIMEOUT)
    if resp.status_code != 200:
        return False
    if not put_object(kind, dst_key, resp.content):
        return False
    return delete_object(kind, src_key)


def public_url(key):
    return f"{public_host()}/{key}"


def sign_private_url(file_url, ttl_seconds=300):
    """Mint a Bunny CDN Token-Authentication (V2, SHA256, query-string) URL.

    hashable = security_key + path + str(expires)
    token    = urlsafe_base64( sha256(hashable) )  with '=' stripped
    url      = {private_host}{path}?token={token}&expires={expires}

    IMPORTANT: the pull zone's Token Authentication settings must match this
    scheme (SHA256, query token, no IP/path locking). Validate against the live
    pull zone before relying on it — see BACKEND_HANDOFF.md §11.
    """
    path = "/" + normalize_key(file_url)
    expires = int(time.time()) + int(ttl_seconds)
    hashable = f"{private_token_key()}{path}{expires}"
    digest = hashlib.sha256(hashable.encode("utf-8")).digest()
    token = base64.b64encode(digest).decode("ascii")
    token = token.replace("\n", "").replace("+", "-").replace("/", "_").replace("=", "")
    return f"{private_host()}{path}?token={token}&expires={expires}"


# =========================================================================== #
# 3. FILE OFFLOAD — File doc_event handlers (wired in hooks.py)
# =========================================================================== #

def _is_managed_url(file_url):
    return bool(file_url) and file_url.startswith(("/files/", "/private/files/"))


def _zone_kind(is_private):
    """Unified: pass the File's is_private value (works for handlers + backfill)."""
    return "private" if int(is_private or 0) else "public"


def _key_allowed_public(key):
    return any(key.startswith(prefix) for prefix in public_folders())


def _content_type(name, key):
    return mimetypes.guess_type(name or key)[0] or "application/octet-stream"


def _read_local(doc):
    """Return the file's bytes from local disk, or None if unavailable."""
    try:
        path = doc.get_full_path()
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        _log(f"could not read local file for {doc.name} ({doc.file_url})\n\n{frappe.get_traceback()}")
        return None


def _delete_local(doc):
    """Remove the local copy after a confirmed Bunny upload. Best-effort — never raises."""
    import os
    try:
        path = doc.get_full_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        _log(f"could not delete local file for {doc.name} ({doc.file_url})\n\n{frappe.get_traceback()}")


def offload(doc):
    """Copy one File to its Bunny zone. Returns True if stored (or already there)."""
    file_url = doc.file_url or ""
    if not _is_managed_url(file_url):
        return False  # external URL / unexpected path — not ours to manage
    if _is_local_only(file_url):
        return False  # pinned to local Frappe storage by bunny_local_patterns

    kind = _zone_kind(doc.is_private)
    if not is_configured(kind):
        return False  # zone not set up yet — stay local

    key = normalize_key(file_url)

    # CODE-level safety: public zone may only ever receive whitelisted prefixes,
    # and a private file may never be routed to the public zone.
    if kind == "public" and not _key_allowed_public(key):
        return False
    if kind == "private" and key.startswith("files/"):
        _log(f"refusing to offload {doc.name}: is_private but key={key}")
        return False

    content = _read_local(doc)
    if content is None:
        return False

    if not put_object(kind, key, content, _content_type(doc.file_name, key)):
        _log(f"Bunny PUT failed for {doc.name} key={key} zone={kind} — kept local")
        return False
    return True


def on_file_after_insert(doc, method=None):
    if not offload_enabled():
        return
    try:
        if offload(doc) and delete_on_upload():
            _delete_local(doc)
    except Exception:
        _log(f"after_insert offload crashed for {getattr(doc, 'name', '?')}\n\n{frappe.get_traceback()}")


def on_file_on_update(doc, method=None):
    """Reconcile a public<->private flip.

    If a file that used to be public is now private, delete its object from the
    PUBLIC zone immediately (leaked-KYC prevention) and push it to the private
    zone. If a private file became public, offload to the public zone.
    """
    if not enabled():
        return
    try:
        file_url = doc.file_url or ""
        if not _is_managed_url(file_url):
            return
        key = normalize_key(file_url)
        if int(doc.is_private or 0):
            if is_configured("public"):
                delete_object("public", key)
            if offload(doc) and delete_on_upload():
                _delete_local(doc)
        else:
            if offload(doc) and delete_on_upload():
                _delete_local(doc)
    except Exception:
        _log(f"on_update reconcile crashed for {getattr(doc, 'name', '?')}\n\n{frappe.get_traceback()}")


def on_file_on_trash(doc, method=None):
    """When Frappe deletes a File, delete the matching Bunny object too."""
    if not enabled():
        return
    try:
        file_url = doc.file_url or ""
        if not _is_managed_url(file_url):
            return
        key = normalize_key(file_url)
        kind = _zone_kind(doc.is_private)
        if is_configured(kind):
            delete_object(kind, key)
        # Defensive: also try the opposite zone in case is_private changed.
        other = "public" if kind == "private" else "private"
        if is_configured(other):
            delete_object(other, key)
    except Exception:
        _log(f"on_trash delete crashed for {getattr(doc, 'name', '?')}\n\n{frappe.get_traceback()}")


# =========================================================================== #
# 4. WHITELISTED API — get_signed_media_url
#
# Replaces the client-side pattern of fetching /private/files/... with the
# Administrator VITE_API_TOKEN (a god-mode key in the browser bundle). The
# frontend calls this with the SESSION cookie; we permission-check server-side
# and return a short-lived signed URL the browser can load directly.
# =========================================================================== #

def _find_file(file_url):
    name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    return frappe.get_doc("File", name) if name else None


def _can_access(file_doc):
    user = frappe.session.user
    if user == "Administrator":
        return True
    if user == "Guest":
        return False
    if file_doc.attached_to_doctype and file_doc.attached_to_name:
        return frappe.has_permission(
            file_doc.attached_to_doctype, "read", doc=file_doc.attached_to_name, user=user
        )
    if file_doc.owner == user:
        return True
    return frappe.has_permission("File", "read", doc=file_doc.name, user=user)


@frappe.whitelist()
def get_signed_media_url(file_url, ttl=None):
    """Return a loadable URL for a media path after a permission check.

    - public /files/...          -> plain CDN URL (no token needed)
    - private /private/files/... -> token-signed private CDN URL, but only if the
      caller may read the backing File / its attached document.
    """
    if not file_url:
        frappe.throw("file_url is required")

    if file_url.startswith("/files/"):
        if is_configured("public"):
            return public_url(normalize_key(file_url))
        return file_url  # not migrated yet — let the frontend resolve on origin

    if not file_url.startswith("/private/files/"):
        frappe.throw("unsupported file_url")

    file_doc = _find_file(file_url)
    if not file_doc:
        raise frappe.DoesNotExistError(f"No File for {file_url}")

    if not _can_access(file_doc):
        raise frappe.PermissionError("Not permitted to access this file")

    if ttl in (None, ""):
        ttl = signed_url_ttl()  # runtime-tunable default
    else:
        try:
            ttl = max(60, min(int(ttl), _MAX_TTL))
        except (TypeError, ValueError):
            ttl = signed_url_ttl()

    if not is_configured("private"):
        # Private zone not set up yet — fall back to the Frappe-served private URL
        # (session-authenticated), so nothing breaks pre-cutover.
        return file_url

    return sign_private_url(file_url, ttl_seconds=ttl)


# =========================================================================== #
# 5. RECONCILE — daily backstop (scheduled in hooks.py)
#
# Ensure no PRIVATE file's bytes sit on the PUBLIC (token-less) zone. The live
# on_file_on_update handler already deletes on a public->private flip; this
# catches missed events (bulk edits, imports). It only ever DELETES from the
# public zone — never the private zone, never local disk — so it cannot lose data.
# =========================================================================== #
_LOOKBACK_DAYS = 3


def run_reconciliation():
    if not reconcile_enabled() or not is_configured("public"):
        return

    private_files = frappe.get_all(
        "File",
        filters={
            "is_private": 1,
            "modified": [">", frappe.utils.add_days(frappe.utils.nowdate(), -_LOOKBACK_DAYS)],
        },
        fields=["name", "file_url"],
        limit_page_length=0,
    )

    purged = 0
    for f in private_files:
        if not (f.file_url or "").startswith(("/files/", "/private/files/")):
            continue
        key = normalize_key(f.file_url)
        exists, _ = head_object("public", key)
        if exists and delete_object("public", key):
            purged += 1

    if purged:
        frappe.logger("yoisho_bunny").info(
            f"reconciliation removed {purged} private-file object(s) from the public zone"
        )


# =========================================================================== #
# 6. BACKFILL — one-time, idempotent, resumable migration of EXISTING files
#
# Run from the bench, e.g.:
#   bench --site <site> execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': True}"
#   bench --site <site> execute yoisho_bunny.bunny.backfill_run --kwargs "{'dry_run': False, 'limit': 500}"
#   bench --site <site> execute yoisho_bunny.bunny.rewrite_absolute_urls --kwargs "{'dry_run': True}"
#   bench --site <site> execute yoisho_bunny.bunny.verify_sample --kwargs "{'n': 100}"
#
# ZERO-404 rule: uploads are checksum-verified by Bunny BEFORE the frontend is
# pointed at the CDN, and file_url stays relative, so nothing is "flipped" in the
# DB. Local originals are NEVER touched here.
# =========================================================================== #
_BATCH = 200

# Known Frappe origins whose absolute /files/ URLs may be baked into content.
_LEGACY_ORIGINS = [
    "https://edu.yoisho.in",
    "https://yoisho-lms.theradixlab.com",
]

# (doctype, fieldname) content fields that may embed absolute /files/ URLs.
_CONTENT_FIELDS = [
    ("Course Lesson", "content"),
    ("LMS Assignment", "question"),
    ("LMS Assignment Submission", "answer"),
    ("LMS Assignment Submission", "comments"),
]


def _has_field(doctype, field):
    try:
        return bool(frappe.get_meta(doctype).get_field(field))
    except Exception:
        return False


def backfill_run(dry_run=True, limit=None):
    """Upload every existing File to its Bunny zone. Resumable (skips matches)."""
    if not enabled():
        print("bunny_enabled is falsy — aborting.")
        return

    stats = {"seen": 0, "skipped": 0, "uploaded": 0, "failed": 0, "not_configured": 0}
    start = 0
    while True:
        rows = frappe.get_all(
            "File",
            filters={"is_folder": 0},
            fields=["name", "file_url", "file_name", "is_private", "file_size"],
            order_by="creation asc",
            limit_start=start,
            limit_page_length=_BATCH,
        )
        if not rows:
            break
        for r in rows:
            stats["seen"] += 1
            if limit and stats["uploaded"] >= limit:
                _print_stats(stats, dry_run)
                return
            _backfill_one(r, dry_run, stats)
        start += _BATCH

    _print_stats(stats, dry_run)


def _backfill_one(r, dry_run, stats):
    file_url = r.get("file_url") or ""
    if not file_url.startswith(("/files/", "/private/files/")):
        stats["skipped"] += 1
        return
    if _is_local_only(file_url):
        stats["skipped"] += 1
        return
    kind = _zone_kind(r.get("is_private"))
    if not is_configured(kind):
        stats["not_configured"] += 1
        return

    key = normalize_key(file_url)
    # Resumability: skip if the object already exists with a matching size.
    exists, size = head_object(kind, key)
    if exists and (r.get("file_size") in (None, 0) or size == r.get("file_size")):
        stats["skipped"] += 1
        return

    if dry_run:
        print(f"[dry-run] would upload {kind}:{key}")
        stats["uploaded"] += 1
        return

    doc = frappe.get_doc("File", r["name"])
    if offload(doc):
        stats["uploaded"] += 1
    else:
        stats["failed"] += 1
        print(f"FAILED {kind}:{key} (see Error Log)")


def _print_stats(stats, dry_run):
    print(f"[{'DRY-RUN' if dry_run else 'LIVE'}] backfill: {stats}")


def rewrite_absolute_urls(dry_run=True):
    """Strip legacy absolute Frappe-origin /files/ URLs back to relative paths.

    Leaves /files/... so the frontend base-swap resolves them via the CDN. This
    is MANDATORY before disk reclamation (Phase 1.5): otherwise these rows keep
    pointing at the origin and 404 once local originals are pruned.
    """
    total = 0
    for doctype, field in _CONTENT_FIELDS:
        if not _has_field(doctype, field):
            continue
        rows = frappe.get_all(doctype, fields=["name", field], limit_page_length=0)
        for row in rows:
            original = row.get(field) or ""
            if not original:
                continue
            updated = original
            for origin in _LEGACY_ORIGINS:
                updated = updated.replace(origin + "/files/", "/files/")
                updated = updated.replace(origin + "/private/files/", "/private/files/")
            if updated != original:
                total += 1
                if dry_run:
                    print(f"[dry-run] {doctype} {row['name']}.{field} would be rewritten")
                else:
                    frappe.db.set_value(doctype, row["name"], field, updated, update_modified=False)
    if not dry_run:
        frappe.db.commit()
    print(f"[{'DRY-RUN' if dry_run else 'LIVE'}] rewrite_absolute_urls: {total} field(s) affected")


@frappe.whitelist()
def verify_sample(n=50):
    """HEAD a sample of migrated public files on Bunny and report misses (Step 5)."""
    frappe.only_for("System Manager")
    rows = frappe.get_all(
        "File",
        filters={"is_folder": 0, "is_private": 0},
        fields=["name", "file_url"],
        order_by="modified desc",
        limit_page_length=int(n),
    )
    missing = []
    for r in rows:
        url = r.get("file_url") or ""
        if not url.startswith("/files/"):
            continue
        key = normalize_key(url)
        exists, _ = head_object("public", key)
        if not exists:
            missing.append(key)
    print(f"verify_sample: checked {len(rows)}, missing {len(missing)}")
    for k in missing:
        print(f"  MISSING {k}")
    return missing


# =========================================================================== #
# 7. PURGE — PHASE 2 (NOT scheduled; ships only after the phase-1 soak)
#
# THE ONE RULE THAT PREVENTS DATA LOSS
# ------------------------------------
# deep_clone_course.py copies image/content fields VERBATIM, so the Original
# course and every batch's "Duplicate" share the SAME /files/<name> bytes / File
# doc. On Bunny the key is byte-identical across all of them. Therefore we NEVER
# delete by walking a batch's course tree. We delete an object ONLY when its
# COMPLETE set of references across the ENTIRE corpus is a strict subset of the
# eligible-batch lineage — nothing outside the doomed batches still points at it.
#
# Selection uses a dedicated, stable LMS Batch.media_frozen_on Custom Field —
# NEVER `modified` (which resets on drip toggles, enrollment sync, timesheet
# auto-publish). run_purge refuses to run until that field exists.
# =========================================================================== #
_GRACE_DAYS = 30
_TRASH_PREFIX = "trash/"

_DIRECTUPLOAD_RE = re.compile(r'"url"\s*:\s*"(/(?:private/)?files/[^"]+)"')
_HTML_SRC_RE = re.compile(r'(?:src|href)=["\'](/(?:private/)?files/[^"\']+)["\']')


def _require_media_frozen_on():
    if not frappe.get_meta("LMS Batch").get_field("media_frozen_on"):
        frappe.throw(
            "LMS Batch.media_frozen_on Custom Field is missing. Create + backfill it "
            "before running the purge (see BACKEND_HANDOFF.md). Purging on `modified` is forbidden."
        )


def _eligible_batches():
    _require_media_frozen_on()
    cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -_GRACE_DAYS)
    return frappe.get_all(
        "LMS Batch",
        filters={"published": 0, "media_frozen_on": ["<", cutoff]},
        fields=["name"],
        limit_page_length=0,
    )


def _keys_from_text(text):
    if not text:
        return set()
    keys = set()
    for m in _DIRECTUPLOAD_RE.finditer(text):
        keys.add(normalize_key(m.group(1)))
    for m in _HTML_SRC_RE.finditer(text):
        keys.add(normalize_key(m.group(1)))
    return keys


def _lineage_keys(batch_name):
    """Every Bunny key reachable from one eligible batch's Duplicate courses."""
    keys = set()
    courses = frappe.get_all(
        "Batch Course Item", filters={"parent": batch_name}, fields=["course"], limit_page_length=0
    )
    for c in courses:
        course = c.get("course")
        if not course:
            continue
        for fld in ("image", "invoice", "live_image", "recorded_image"):
            val = frappe.db.get_value("Yoisho Course", course, fld) if _has_field("Yoisho Course", fld) else None
            if val:
                keys.add(normalize_key(val))
        chapters = frappe.get_all(
            "Course Chapter", filters={"course": course}, fields=["name"], limit_page_length=0
        )
        for ch in chapters:
            lessons = frappe.get_all(
                "Course Lesson", filters={"chapter": ch["name"]}, fields=["name", "content"], limit_page_length=0
            )
            for les in lessons:
                keys |= _keys_from_text(les.get("content"))
    return keys


def _build_reference_index():
    """{normalized_key -> number of references across the WHOLE corpus}.

    Sources (must stay EXHAUSTIVE — an omission risks deleting live media):
      - every File.file_url
      - Course Lesson.content (directupload + inline HTML)
      - Yoisho Course Attach + in-scope Data-field video URLs
      - User.user_image
    TODO before enabling in prod: add quiz question audio_url/image_url and any
    other media-bearing DocType/field; validate completeness against a known set.
    """
    idx = {}

    def add(url):
        if url and str(url).startswith(("/files/", "/private/files/")):
            k = normalize_key(url)
            idx[k] = idx.get(k, 0) + 1

    for f in frappe.get_all("File", filters={"is_folder": 0}, fields=["file_url"], limit_page_length=0):
        add(f.get("file_url"))

    for les in frappe.get_all("Course Lesson", fields=["content"], limit_page_length=0):
        for k in _keys_from_text(les.get("content")):
            idx[k] = idx.get(k, 0) + 1

    for fld in ("image", "invoice", "live_image", "recorded_image", "video_url", "recorded_video_url"):
        if not _has_field("Yoisho Course", fld):
            continue
        for row in frappe.get_all("Yoisho Course", fields=["name", fld], limit_page_length=0):
            add(row.get(fld))

    if _has_field("User", "user_image"):
        for u in frappe.get_all("User", fields=["user_image"], limit_page_length=0):
            add(u.get("user_image"))

    return idx


@frappe.whitelist()
def run_purge(dry_run=True, confirm_token=None):
    """Compute deletable keys and (in LIVE mode) SOFT-delete them to trash/.

    Deletion criterion: key's TOTAL corpus reference count <= references coming
    only from eligible-batch lineage. Anything referenced elsewhere is spared.
    """
    frappe.only_for("System Manager")
    if not enabled() or not is_configured("public"):
        return {"error": "bunny not configured"}

    batches = _eligible_batches()
    if not batches:
        return {"eligible_batches": 0, "candidates": 0}

    lineage_all = {}
    for b in batches:
        # Re-check published==0 right now (re-publish race).
        if frappe.db.get_value("LMS Batch", b["name"], "published"):
            continue
        for k in _lineage_keys(b["name"]):
            lineage_all[k] = lineage_all.get(k, 0) + 1

    ref_index = _build_reference_index()

    deletable = []
    for key, lineage_refs in lineage_all.items():
        total_refs = ref_index.get(key, 0)
        # Strict-subset test: the ONLY references are from doomed batches.
        if total_refs and total_refs <= lineage_refs:
            deletable.append(key)

    manifest = {
        "eligible_batches": len(batches),
        "candidate_keys": len(deletable),
        "keys": sorted(deletable),
    }

    if dry_run:
        _write_manifest(manifest)
        return {"dry_run": True, **{k: manifest[k] for k in ("eligible_batches", "candidate_keys")}}

    if confirm_token != _expected_confirm_token(deletable):
        return {
            "error": "hard run requires confirm_token matching the reviewed dry-run manifest",
            "expected_confirm_token": _expected_confirm_token(deletable),
        }

    moved = 0
    for key in deletable:
        # SOFT delete: move to trash/<date>/ for a 30-day grace; never hard delete here.
        dst = f"{_TRASH_PREFIX}{frappe.utils.nowdate()}/{key}"
        if move_object("public", key, dst):
            moved += 1
            _purge_cdn_cache(key)
    return {"dry_run": False, "soft_deleted": moved, "candidate_keys": len(deletable)}


def _expected_confirm_token(deletable):
    return hashlib.sha256("\n".join(sorted(deletable)).encode()).hexdigest()[:16]


def _write_manifest(manifest):
    frappe.logger("yoisho_bunny").info(f"purge dry-run manifest: {manifest}")
    # A "Bunny Purge Log" DocType (to persist manifests for sign-off) is a
    # phase-2 deliverable — see BACKEND_HANDOFF.md.


def _purge_cdn_cache(key):
    """Best-effort CDN cache purge so edge copies drop before TTL."""
    pull_zone = frappe.conf.get("bunny_public_pullzone_id")
    api_key = frappe.conf.get("bunny_api_key")
    if not (pull_zone and api_key):
        return
    try:
        requests.post(
            "https://api.bunny.net/purge",
            params={"url": public_url(key)},
            headers={"AccessKey": api_key},
            timeout=(5, 15),
        )
    except Exception:
        _log(frappe.get_traceback())


# =========================================================================== #
# 8. CONTROL PLANE — operate WITHOUT server/SSH access.
#
# Every function here is @frappe.whitelist() + System-Manager-guarded, so it can
# be driven from:
#   * Desk    : a Client Script button -> frappe.call({method: "...", args: {...}})
#   * REST    : POST /api/method/yoisho_bunny.bunny.<fn>   (Authorization: token ...)
#   * Server Script (type "API"/"Scheduler") : frappe.call is limited under
#               safe_exec, but you can flip the master switch directly with
#               frappe.db.set_default("bunny_enabled", "0")  (see disable() below).
#
# What you CAN change without a redeploy: enable/disable (master + per-feature),
# region/hosts/zones, signed-URL TTL, public folders, and triggering
# backfill/verify/reconcile/purge.
# What still needs a file edit + `bench restart`: the core Bunny I/O logic itself
# (uploading bytes over HTTP cannot run in safe_exec). Keep tweaks to config, not code.
# =========================================================================== #

@frappe.whitelist()
def status():
    """Current effective config (NO secrets) — for debugging from Desk/REST."""
    frappe.only_for("System Manager")
    return {
        "enabled": enabled(),
        "offload_enabled": offload_enabled(),
        "reconcile_enabled": reconcile_enabled(),
        "region": region(),
        "public_host": public_host(),
        "private_host": private_host(),
        "public_zone": zone("public"),
        "private_zone": zone("private"),
        "public_folders": public_folders(),
        "signed_url_ttl": signed_url_ttl(),
        "public_configured": is_configured("public"),
        "private_configured": is_configured("private"),
    }


@frappe.whitelist()
def enable():
    frappe.only_for("System Manager")
    frappe.db.set_default("bunny_enabled", "1")
    return {"enabled": True}


@frappe.whitelist()
def disable():
    """Master kill-switch OFF. Existing Bunny objects keep serving; new uploads stay local."""
    frappe.only_for("System Manager")
    frappe.db.set_default("bunny_enabled", "0")
    return {"enabled": False}


@frappe.whitelist()
def set_setting(name, value):
    """Change a NON-SECRET runtime setting (see _TUNABLE). Secrets are refused."""
    frappe.only_for("System Manager")
    if name not in _TUNABLE:
        frappe.throw(f"'{name}' is not runtime-tunable (secrets live in site_config.json only)")
    frappe.db.set_default(f"bunny_{name}", str(value))
    return {name: _setting(name)}


@frappe.whitelist()
def reconcile_now():
    """Run the private-on-public reconciliation sweep on demand."""
    frappe.only_for("System Manager")
    run_reconciliation()
    return {"ok": True}


@frappe.whitelist()
def backfill_start(dry_run=1, limit=None):
    """Enqueue the backfill as a background job (safe for long runs)."""
    frappe.only_for("System Manager")
    frappe.enqueue(
        "yoisho_bunny.bunny.backfill_run",
        queue="long", timeout=36000, job_name="bunny_backfill",
        dry_run=_truthy(dry_run), limit=limit,
    )
    return {"queued": "backfill_run", "dry_run": _truthy(dry_run), "limit": limit}


@frappe.whitelist()
def rewrite_absolute_urls_start(dry_run=1):
    """Enqueue the legacy absolute-URL rewrite as a background job."""
    frappe.only_for("System Manager")
    frappe.enqueue(
        "yoisho_bunny.bunny.rewrite_absolute_urls",
        queue="long", timeout=36000, job_name="bunny_rewrite_urls",
        dry_run=_truthy(dry_run),
    )
    return {"queued": "rewrite_absolute_urls", "dry_run": _truthy(dry_run)}


# =========================================================================== #
# 9. ASSETS LIBRARY / MANUAL CONTROL
#
# Fine-grained per-file operations and Phase-1.5 disk reclamation — all
# @frappe.whitelist() + System-Manager-gated so they can be triggered from
# Server Scripts or Desk without SSH.
# =========================================================================== #

@frappe.whitelist()
def offload_one(file_name, force=0):
    """Manually offload a single File doc to its Bunny zone.

    By default skips if the object already exists on Bunny (idempotent).
    Pass force=1 to re-upload regardless (useful if the remote copy is corrupt).
    Returns {"uploaded": bool, "reason": str|None}.
    """
    frappe.only_for("System Manager")
    doc = frappe.get_doc("File", file_name)
    kind = _zone_kind(doc.is_private)
    if not is_configured(kind):
        return {"uploaded": False, "reason": f"{kind} zone not configured"}
    if not int(force):
        key = normalize_key(doc.file_url or "")
        exists, _ = head_object(kind, key)
        if exists:
            return {"uploaded": False, "reason": "already_on_bunny"}
    result = offload(doc)
    return {"uploaded": result, "reason": None if result else "offload_failed_see_error_log"}


@frappe.whitelist()
def check_file(file_url):
    """HEAD-check a single file on Bunny. Returns exists, size, zone, and CDN URL."""
    frappe.only_for("System Manager")
    if not _is_managed_url(file_url):
        return {"error": "not a managed url"}
    key = normalize_key(file_url)
    kind = "private" if file_url.startswith("/private/") else "public"
    if not is_configured(kind):
        return {"error": f"{kind} zone not configured"}
    exists, size = head_object(kind, key)
    result = {"exists": exists, "size": size, "key": key, "zone": kind}
    if exists and kind == "public":
        result["cdn_url"] = public_url(key)
    return result


@frappe.whitelist()
def list_bunny_files(kind="public", path_prefix="", page=1):
    """List objects in a Bunny storage zone (uses native Edge Storage listing API).

    Returns up to 1000 objects per call. Use path_prefix to drill into a folder
    (e.g. "files/", "private/files/"). Useful for the Assets Library browser.
    """
    frappe.only_for("System Manager")
    if kind not in ("public", "private"):
        frappe.throw("kind must be 'public' or 'private'")
    if not is_configured(kind):
        return {"error": f"{kind} zone not configured"}
    path = (path_prefix or "").strip("/")
    base = f"https://{_storage_host()}/{zone(kind)}/"
    url = f"{base}{path}/" if path else base
    resp = requests.get(url, headers=_headers(kind), timeout=_TIMEOUT)
    if resp.status_code != 200:
        return {"error": f"Bunny returned {resp.status_code}", "zone": kind}
    items = resp.json()
    return {"items": items, "count": len(items), "zone": kind, "path_prefix": path_prefix}


@frappe.whitelist()
def delete_one(file_url, kind=None):
    """Delete a single object from Bunny storage.

    Does NOT delete the Frappe File doc — use this for manual cleanup only.
    Pass kind='public'|'private' to override auto-detection from the URL.
    """
    frappe.only_for("System Manager")
    if not _is_managed_url(file_url):
        return {"error": "not a managed url"}
    key = normalize_key(file_url)
    if kind not in ("public", "private"):
        kind = "private" if file_url.startswith("/private/") else "public"
    if not is_configured(kind):
        return {"error": f"{kind} zone not configured"}
    ok = delete_object(kind, key)
    return {"deleted": ok, "key": key, "zone": kind}


@frappe.whitelist()
def purge_cdn_url(file_url):
    """Manually purge a specific file URL from the public CDN cache."""
    frappe.only_for("System Manager")
    if not is_configured("public"):
        return {"error": "public zone not configured"}
    key = normalize_key(file_url)
    _purge_cdn_cache(key)
    return {"purged": key, "cdn_url": public_url(key)}


# =========================================================================== #
# 10. PHASE 1.5 — LOCAL DISK RECLAMATION
#
# Run ONLY after backfill_start (live) + verify_sample confirm every file is on
# Bunny. For each local file, HEAD Bunny first — skip if missing (never deletes
# without proof). Safe to re-run: already-deleted files are a no-op (skipped).
# =========================================================================== #

def reclaim_local(dry_run=True, limit=None):
    """Delete local originals of PUBLIC files that are verified to exist on Bunny.

    Only reclaims public files (is_private=0 / /files/ paths). Private files
    are intentionally excluded: they are served by Frappe with auth checks and
    a CDN redirect for private paths is not yet implemented.  Running reclaim on
    private files would make them inaccessible until signed-URL serving is wired
    to the frontend — so Phase 1 reclaim is public-only.

    HEAD-checks each file on Bunny before deleting; never deletes a file that
    Bunny returns 404 for.
    """
    import os

    if not enabled():
        print("bunny_enabled is falsy — aborting.")
        return

    stats = {"seen": 0, "reclaimed": 0, "skipped": 0, "not_on_bunny": 0, "failed": 0}
    start = 0
    while True:
        rows = frappe.get_all(
            "File",
            filters={"is_folder": 0, "is_private": 0},
            fields=["name", "file_url", "is_private", "file_size"],
            order_by="creation asc",
            limit_start=start,
            limit_page_length=_BATCH,
        )
        if not rows:
            break
        for r in rows:
            stats["seen"] += 1
            if limit and stats["reclaimed"] >= int(limit):
                _print_reclaim_stats(stats, dry_run)
                return
            _reclaim_one(r, dry_run, stats)
        start += _BATCH

    _print_reclaim_stats(stats, dry_run)


def _reclaim_one(r, dry_run, stats):
    import os

    file_url = r.get("file_url") or ""
    if not file_url.startswith(("/files/", "/private/files/")):
        stats["skipped"] += 1
        return

    kind = _zone_kind(r.get("is_private"))
    if not is_configured(kind):
        stats["skipped"] += 1
        return

    key = normalize_key(file_url)
    exists, _ = head_object(kind, key)
    if not exists:
        stats["not_on_bunny"] += 1
        print(f"NOT ON BUNNY — skipping {kind}:{key}")
        return

    try:
        doc = frappe.get_doc("File", r["name"])
        local_path = doc.get_full_path()
    except Exception:
        stats["skipped"] += 1
        return

    if not os.path.exists(local_path):
        stats["skipped"] += 1
        return

    if dry_run:
        print(f"[dry-run] would delete {local_path}  ({kind}:{key} verified on Bunny)")
        stats["reclaimed"] += 1
        return

    try:
        os.remove(local_path)
        stats["reclaimed"] += 1
    except Exception:
        stats["failed"] += 1
        _log(f"reclaim_local: could not delete {local_path}\n\n{frappe.get_traceback()}")


def _print_reclaim_stats(stats, dry_run):
    label = "DRY-RUN" if dry_run else "LIVE"
    print(f"[{label}] reclaim_local: {stats}")
    summary = (
        f"[{label}] reclaimed={stats['reclaimed']}  "
        f"not_on_bunny={stats['not_on_bunny']}  "
        f"skipped={stats['skipped']}  failed={stats['failed']}  "
        f"seen={stats['seen']}"
    )
    try:
        frappe.publish_realtime(
            event="bunny_reclaim_done",
            message={"summary": summary, "stats": stats, "dry_run": dry_run},
            user="Administrator",
        )
        frappe.get_doc({
            "doctype": "Notification Log",
            "subject": f"Bunny reclaim_local [{label}] complete",
            "email_content": summary,
            "type": "Alert",
            "for_user": "Administrator",
            "document_type": "File",
            "document_name": "",
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass  # notification is best-effort — never block the reclaim result


@frappe.whitelist()
def reclaim_local_start(dry_run=1, limit=None):
    """Enqueue Phase-1.5 local disk reclamation as a background job."""
    frappe.only_for("System Manager")
    frappe.enqueue(
        "yoisho_bunny.bunny.reclaim_local",
        queue="long", timeout=36000, job_name="bunny_reclaim_local",
        dry_run=_truthy(dry_run), limit=limit,
    )
    return {"queued": "reclaim_local", "dry_run": _truthy(dry_run), "limit": limit}


@frappe.whitelist()
def cleanup_orphans(dry_run=True):
    """Delete File records where the file exists neither on local disk nor on Bunny.

    Safe to run multiple times. Always dry_run first to review what will be deleted.
    """
    import os
    frappe.only_for("System Manager")
    dry_run = _truthy(dry_run) if isinstance(dry_run, str) else bool(dry_run)

    files = frappe.get_all(
        "File",
        filters={"is_folder": 0},
        fields=["name", "file_url", "file_name", "is_private", "file_size"],
        limit_page_length=0,
    )

    stats = {"seen": 0, "orphans": 0, "deleted": 0, "kept": 0}

    for f in files:
        file_url = f.get("file_url") or ""
        if not file_url.startswith(("/files/", "/private/files/")):
            continue

        stats["seen"] += 1

        # Check local disk
        try:
            doc = frappe.get_doc("File", f["name"])
            on_disk = os.path.exists(doc.get_full_path())
        except Exception:
            on_disk = False

        # Check Bunny
        kind = _zone_kind(f.get("is_private"))
        key = normalize_key(file_url)
        on_bunny, _ = head_object(kind, key)

        if not on_disk and not on_bunny:
            stats["orphans"] += 1
            print(f"ORPHAN: {f['name']} {file_url}")
            if not dry_run:
                try:
                    frappe.delete_doc("File", f["name"], ignore_permissions=True)
                    stats["deleted"] += 1
                except Exception:
                    _log(f"cleanup_orphans: could not delete {f['name']}\n\n{frappe.get_traceback()}")
        else:
            stats["kept"] += 1

    if not dry_run:
        frappe.db.commit()

    label = "DRY-RUN" if dry_run else "LIVE"
    print(f"[{label}] cleanup_orphans: {stats}")
    return stats


# =========================================================================== #
# 11. CDN FALLBACK — redirect /files/... to Bunny when local copy is gone
# =========================================================================== #
#
# When a /files/ request arrives, StaticDataMiddleware (a Werkzeug
# SharedDataMiddleware subclass) tries to serve the file from disk.  If the
# file is missing it does NOT raise NotFound — it falls through to the Frappe
# Flask app, which tries to open a DB connection.  If MariaDB is under load
# ("Too many connections") that results in a 500, not a 404.
#
# Fix: patch StaticDataMiddleware.__call__ (a CLASS method, so Python's dynamic
# method lookup means the patch applies to the already-created instance too).
# The wrapper PRE-CHECKS whether the local file exists BEFORE calling through
# to Flask.  If the file is missing and bunny_enabled=1, it issues a 302 to
# the Bunny CDN URL — reading site_config.json directly, never touching the DB.
#
# This module is imported on the first request (after StaticDataMiddleware is
# already instantiated), so patching __call__ — not __init__ or
# get_directory_loader — is the only approach that works.
#
# Scope: covers bench serve (dev) and gunicorn (prod).
# No nginx/apache configuration required — the patch handles all /files/ 404s
# at the Python WSGI layer.


def _bunny_bench_root():
    """Return the absolute path to the bench root, derived from frappe's location."""
    import frappe as _f
    import os
    return os.path.abspath(os.path.join(os.path.dirname(_f.__file__), "..", "..", ".."))


def _bunny_resolve_site(environ):
    """
    Determine the Frappe site name in a WSGI middleware context (no Frappe
    request context available yet).  Tries five sources in order:
    1. frappe.app._site  — set when bench is started with --site
    2. sites/currentsite.txt  — written by bench in single-site setups
    3. X-Frappe-Site-Name request header  — sent by nginx in multi-site setups
    4. HTTP_HOST header stripping port  — works in production with named vhosts
    5. Single-site auto-detect — only one site_config.json under sites/
    """
    import os
    bench = _bunny_bench_root()
    sites_root = os.path.join(bench, "sites")

    # 1. frappe.app._site
    try:
        import frappe.app as _fa
        site = getattr(_fa, "_site", None) or ""
        if site:
            return site
    except Exception:
        pass
    # 2. sites/currentsite.txt
    try:
        with open(os.path.join(sites_root, "currentsite.txt")) as f:
            site = f.read().strip()
        if site:
            return site
    except Exception:
        pass
    # 3. X-Frappe-Site-Name header (nginx may inject this)
    site = environ.get("HTTP_X_FRAPPE_SITE_NAME", "").strip()
    if site:
        return site
    # 4. HTTP_HOST (works for named vhosts, not IP-based dev access)
    host = environ.get("HTTP_HOST", "")
    site = host.split(":")[0] if host else ""
    if site and os.path.exists(os.path.join(sites_root, site, "site_config.json")):
        return site
    # 5. Single-site bench — exactly one directory with a site_config.json
    try:
        candidates = [
            e for e in os.listdir(sites_root)
            if os.path.isdir(os.path.join(sites_root, e))
            and os.path.exists(os.path.join(sites_root, e, "site_config.json"))
        ]
        if len(candidates) == 1:
            return candidates[0]
    except Exception:
        pass
    return ""


def _apply_cdn_fallback_patch():
    """
    Wrap StaticDataMiddleware.__call__ once so that missing /files/ are
    redirected to the Bunny CDN (302) when bunny_enabled=1, without ever
    touching the database.  Idempotent — safe to call multiple times.
    """
    try:
        from frappe.middlewares import StaticDataMiddleware
        if getattr(StaticDataMiddleware, "_bunny_patched", False):
            return

        _original_call = StaticDataMiddleware.__call__

        def _bunny_call(self, environ, start_response):
            import json
            import os
            import logging as _log
            from werkzeug.exceptions import NotFound

            path = environ.get("PATH_INFO", "")

            # Pre-check: if the file is missing locally for a /files/ path,
            # redirect to Bunny BEFORE falling through to Flask (which would
            # try to open a DB connection and may 500 under load).
            if path.startswith("/files/"):
                try:
                    site = _bunny_resolve_site(environ)
                    if site:
                        bench_root = _bunny_bench_root()
                        local_path = os.path.join(
                            bench_root, "sites", site, "public", path.lstrip("/")
                        )
                        if not os.path.exists(local_path):
                            config_path = os.path.join(
                                bench_root, "sites", site, "site_config.json"
                            )
                            with open(config_path) as _f:
                                cfg = json.load(_f)

                            if cfg.get("bunny_enabled") and cfg.get("bunny_public_host"):
                                # Respect bunny_local_patterns — never redirect
                                # files that are pinned to local storage.
                                raw_lp = cfg.get("bunny_local_patterns", [])
                                if isinstance(raw_lp, str) and raw_lp.strip():
                                    try:
                                        raw_lp = json.loads(raw_lp)
                                    except Exception:
                                        raw_lp = [
                                            p.strip()
                                            for p in raw_lp.strip("[] ").replace('"', "").split(",")
                                            if p.strip()
                                        ]
                                local_patterns = list(raw_lp) if isinstance(raw_lp, list) else []

                                if not any(path.startswith(p) for p in local_patterns):
                                    cdn_url = cfg["bunny_public_host"].rstrip("/") + path
                                    qs = environ.get("QUERY_STRING", "")
                                    if qs:
                                        cdn_url += "?" + qs
                                    from werkzeug.utils import redirect as _redirect
                                    return _redirect(cdn_url, 302)(environ, start_response)
                except Exception as _exc:
                    _log.getLogger("yoisho_bunny").warning(
                        "CDN fallback pre-check error for %s: %s", path, _exc
                    )

            return _original_call(self, environ, start_response)

        StaticDataMiddleware.__call__ = _bunny_call
        StaticDataMiddleware._bunny_patched = True
    except Exception as _e:
        import logging as _logging
        _logging.getLogger("yoisho_bunny").warning(
            "yoisho_bunny: CDN fallback patch failed — /files/ requests will "
            "not redirect to Bunny if local files are missing. Error: %s", _e
        )


def _cdn_redirect_before_request():
    """
    Frappe before_request hook — fallback CDN redirect for /files/ misses that
    reach Flask before the StaticDataMiddleware patch is active (e.g., first
    request after a process restart).

    The StaticDataMiddleware patch (above) is the primary redirect path and
    requires no DB.  This hook covers the cold-start gap: it runs inside
    Flask's request pipeline (after frappe.connect()) and returns a redirect
    response before Frappe's own file-serving logic can raise an exception.
    """
    import os
    try:
        path = frappe.local.request.path if hasattr(frappe.local, "request") else ""
        if not path.startswith("/files/"):
            return

        site = frappe.local.site
        if not site:
            return

        bench_path = frappe.utils.get_bench_path()
        local_path = os.path.join(bench_path, "sites", site, "public", path.lstrip("/"))
        if os.path.exists(local_path):
            return  # file exists locally — serve normally

        cfg = frappe.conf
        if not cfg.get("bunny_enabled") or not cfg.get("bunny_public_host"):
            return

        raw_lp = cfg.get("bunny_local_patterns", [])
        if isinstance(raw_lp, str) and raw_lp.strip():
            try:
                import json as _json
                raw_lp = _json.loads(raw_lp)
            except Exception:
                raw_lp = [
                    p.strip()
                    for p in raw_lp.strip("[] ").replace('"', "").split(",")
                    if p.strip()
                ]
        if any(path.startswith(p) for p in (raw_lp if isinstance(raw_lp, list) else [])):
            return

        cdn_url = cfg.get("bunny_public_host").rstrip("/") + path
        qs = frappe.local.request.query_string.decode("utf-8", errors="replace")
        if qs:
            cdn_url += "?" + qs

        from werkzeug.utils import redirect as _redirect
        return _redirect(cdn_url, 302)
    except Exception:
        return  # never break the request pipeline


_apply_cdn_fallback_patch()
