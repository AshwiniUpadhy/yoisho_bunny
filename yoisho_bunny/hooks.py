app_name = "yoisho_bunny"
app_title = "Yoisho Bunny"
app_publisher = "Yoisho"
app_description = "Offload Frappe LMS File storage to Bunny.net Edge Storage + CDN"
app_email = "dev@yoisho.in"
app_license = "MIT"

# All logic lives in one module: yoisho_bunny/bunny.py

# ---------------------------------------------------------------------------
# File lifecycle -> Bunny offload
#
# We hook the standard File doc events rather than override_doctype_class so
# Frappe still writes the local copy first (our fallback / safety net). The
# handlers are defensive: they NEVER raise, so /method/upload_file can't fail
# because of Bunny. The {message:{file_url,file_name}} response contract the
# frontend depends on (src/api/fileApi.js) is therefore untouched.
# ---------------------------------------------------------------------------
doc_events = {
    "File": {
        "after_insert": "yoisho_bunny.bunny.on_file_after_insert",
        "on_update": "yoisho_bunny.bunny.on_file_on_update",
        "on_trash": "yoisho_bunny.bunny.on_file_on_trash",
    }
}

# ---------------------------------------------------------------------------
# Scheduled reconciliation: delete any PUBLIC-zone object whose backing File is
# now private (a public->private flip the after_insert guard can't see). Cheap;
# only ever deletes from the public zone. Respects the reconcile_enabled flag.
#
# NOTE: the phase-2 media PURGE (yoisho_bunny.bunny.run_purge) is intentionally
# NOT scheduled here yet — it ships separately after the phase-1 soak.
# ---------------------------------------------------------------------------
scheduler_events = {
    "daily": [
        "yoisho_bunny.bunny.run_reconciliation",
    ],
    # "daily_long": ["yoisho_bunny.bunny.run_purge"],   # phase 2 — enable later
}

# ---------------------------------------------------------------------------
# CDN fallback: redirect /files/ misses to Bunny before Frappe's handler runs.
# This covers the cold-start gap where the StaticDataMiddleware patch hasn't
# been applied yet (first request after a process restart).
# ---------------------------------------------------------------------------
before_request = ["yoisho_bunny.bunny._cdn_redirect_before_request"]

# Force bunny.py to be imported (and StaticDataMiddleware to be patched) as
# soon as any request loads these hooks — not just when a File doc event fires.
try:
    from yoisho_bunny.bunny import _apply_cdn_fallback_patch as _p; _p()
except Exception:
    pass
