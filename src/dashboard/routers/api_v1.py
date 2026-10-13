"""/api/v1 router (9.3, deep dive §9.3, extended by C22 lines 980-993):
JSON API for external integration (n8n, client scripts, future
frontends). 9.3 deliberately left it read-only ("the formal foundation
that C22 will build writes on later") -- this module is that
extension: PATCH /alerts/{id} (the only write), per-key scopes, per-key
rate limit, and the read endpoints below.

Authentication via the `X-API-Key` header (NOT via the web session cookie,
see this route's bypass in dashboard.main.auth_middleware): a valid API
key coexists with, but doesn't depend on, the web session -- an external
script never has nor needs a `sid`. The key is verified in
database.models.Database.verify_api_key with constant-time comparison
(hmac.compare_digest), and it returns the same 401 whether the key
doesn't exist or is revoked -- see require_api_key below, that's where
that indistinguishable 401 is decided.

The Pydantic `response_model` on each endpoint is what makes the JSON
"typed" (acceptance 9.3 #2) and what fills /docs with a real schema
instead of an opaque `dict`. All read endpoints reuse helpers that
ALREADY exist in database.models -- ZERO new SQL in this router (same
approach as 9.3, extended to C22: "don't invent new tables or queries").

Scopes (C22): 'read' (default, enough for ANY GET on this router, same
as in 9.3), 'write:alerts' (the only write: PATCH /alerts/{id}), and
'admin' (a ceiling that covers any scope, including write:alerts).
Special care from the C22 brief: an INVALID key -> 401 (require_api_key);
a VALID key without the required scope -> 403 (require_scope, below) --
these are not the same case and must not be confused, anyone debugging
from outside needs to be able to tell "my key is wrong" apart from
"my key doesn't have permission".

Subjects (multi-subject): every endpoint takes an optional `subject` query
parameter (a subject id) and every response row carries `subject_id`.
  * A key PINNED to a subject (api_keys.subject_id set, chosen in Settings →
    API keys) always reads/writes that subject; `subject` may be omitted or
    equal to it, any other value -> 403.
  * A deployment-wide key ("All subjects") uses `subject` when given and
    falls back to subject 1 when omitted -- backward compatible with
    integrations written before subjects existed, even when several subjects
    are active (decision: never break an existing n8n flow; pass `subject`
    to read another one).
  * An unknown or inactive subject id -> 404.
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from apify.actors import ACTORS, VALID_DEVICES
from dashboard.deps import db
from database.base import DEFAULT_SUBJECT_ID, subject_scope

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


@router.get("/docs", include_in_schema=False)
def api_v1_docs_redirect():
    """Convenience redirect from /api/v1/docs to FastAPI's interactive OpenAPI documentation (/docs)."""
    return RedirectResponse(url="/docs", status_code=307)


# Same caps as the rest of the repo (H14/2.2 in dashboard.main): a
# hand-crafted query param must never be able to request an entire table
# or an absurd offset -- it's silently clamped instead of returning 422/500.
_MAX_LIMIT = 200
_MAX_OFFSET = 5000

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Simple per-key rate limit (C22, deep dive §C22: "the brake that keeps
# a misbehaving flow from draining the quota"). Fixed in-memory window
# (NOT a sliding window -- enough for the real use case, a misbehaving
# client in a tight loop, same simplicity approach as the rest of the
# repo): {key_id: (window_start_monotonic, count)}, 60s window.
# Lives in process memory on purpose (# ponytail: a service restart
# resets the counter, tolerable -- no need to persist it in the DB for
# an anti-abuse brake). `_RATE_LIMIT_PER_MIN` is a MODULE attribute (not
# a `_` constant) on purpose: tests monkeypatch it to a low value so they
# don't have to fire 120 real requests.
_RATE_LIMIT_PER_MIN = 120
_rate_window: dict[int, tuple[float, int]] = {}


def _check_rate_limit(key_id: int) -> None:
    now = time.monotonic()
    window_start, count = _rate_window.get(key_id, (now, 0))
    if now - window_start >= 60.0:
        window_start, count = now, 0
    count += 1
    _rate_window[key_id] = (window_start, count)
    if count > _RATE_LIMIT_PER_MIN:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded ({_RATE_LIMIT_PER_MIN} req/min per API key)",
        )


def require_api_key(request: Request, api_key: str | None = Depends(_api_key_header)) -> dict:
    """FastAPI dependency: validates X-API-Key, checks revoked_at (inside
    verify_api_key), applies the per-key rate limit (C22), and updates
    last_used WITHOUT blocking the response if that write fails
    (touch_api_key_last_used is already best-effort, it never raises).

    Special care (9.3 brief): no key, a made-up key, and a revoked key all
    return the SAME 401 -- neither the status nor the detail gives any hint
    about which keys actually exist. `scope` is NOT checked here -- that's
    require_scope's job (C22, below): this dependency only answers "is the
    key itself valid?" (401 if not), any READ-ONLY endpoint accepts any
    non-revoked key, same as in 9.3."""
    row = db.verify_api_key(api_key or "")
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    _check_rate_limit(row["id"])
    db.touch_api_key_last_used(row["id"])
    return row


def require_scope(required_scope: str):
    """FastAPI dependency factory (C22): besides requiring a valid key
    (require_api_key, which already decides the 401), it requires that its
    `scope` be EXACTLY `required_scope` or 'admin' (a ceiling that covers
    any scope) -- otherwise, 403. Deliberate distinction from the brief: an
    INVALID key never reaches here (require_api_key already cut it off with
    401 before); a VALID key without the required permission is a DIFFERENT
    case (403) -- "an invalid key is 401, a valid key without permission is
    403, don't mix them up"."""
    def _dependency(row: dict = Depends(require_api_key)) -> dict:
        if row.get("scopes") not in (required_scope, "admin"):
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: {required_scope}",
            )
        return row
    return _dependency


_SUBJECT_PARAM = Query(
    None, ge=1,
    description="Subject id. Optional: a key pinned to a subject always uses it (another "
                "value -> 403); an 'All subjects' key defaults to subject 1.",
)


def resolve_api_subject(key: dict, subject: int | None) -> int:
    """The subject id an API call works on (see the module docstring):
    pinned key -> its subject (a different `subject` -> 403); otherwise
    `subject` or DEFAULT_SUBJECT_ID. 404 if that subject doesn't exist or is
    inactive."""
    pinned = key.get("subject_id")
    if pinned is not None:
        if subject is not None and int(subject) != int(pinned):
            raise HTTPException(status_code=403,
                                detail=f"This API key is limited to subject {pinned}")
        sid = int(pinned)
    else:
        sid = int(subject) if subject is not None else DEFAULT_SUBJECT_ID
    row = db.get_subject(sid)
    if row is None or not row["active"]:
        raise HTTPException(status_code=404, detail="subject not found")
    return sid


def api_subject(subject: int | None = _SUBJECT_PARAM, key: dict = Depends(require_api_key)) -> int:
    return resolve_api_subject(key, subject)


def _with_subject(rows, sid: int):
    if isinstance(rows, dict):
        return {**rows, "subject_id": sid}
    return [{**r, "subject_id": sid} for r in rows]


def _clamp_limit(limit: int) -> int:
    return min(max(limit, 1), _MAX_LIMIT)


def _clamp_offset(offset: int) -> int:
    return min(max(offset, 0), _MAX_OFFSET)


# --------------------------------------------------------------------- #
# Response models (schemas.py doesn't exist in the repo yet -- they're
# defined here, alongside the only router that uses them)
# --------------------------------------------------------------------- #
class ResultOut(BaseModel):
    id: int
    subject_id: int
    run_id: str | None = None
    url: str | None = None
    title: str | None = None
    domain: str | None = None
    country_code: str | None = None
    engine: str | None = None
    device: str | None = None
    classification: str | None = None
    severity: int | None = None
    position: int | None = None
    scraped_at: str | None = None


class RunOut(BaseModel):
    id: int
    subject_id: int
    apify_run_id: str | None = None
    country_code: str | None = None
    status: str | None = None
    engine: str | None = None
    total_results: int | None = None
    positive: int | None = None
    negative: int | None = None
    neutral: int | None = None
    created_at: str | None = None


class AlertOut(BaseModel):
    id: int
    subject_id: int
    created_at: str | None = None
    kind: str | None = None
    severity: str | None = None
    country: str | None = None
    message: str | None = None
    status: str | None = None
    seen: int | None = None


class StatsOut(BaseModel):
    subject_id: int
    total_runs: int
    total_results: int
    by_class: dict[str, int]
    last_run: str | None = None
    countries: int



# C22 (acceptance #1): body of PATCH /alerts/{id} -- reuses the 7.2
# lifecycle (Database.ack_alert/resolve_alert) instead of reimplementing
# it, so it only exposes the TWO transitions those helpers already support.
_ALERT_PATCH_STATUSES = ("acknowledged", "resolved")


class AlertStatusIn(BaseModel):
    # Explicit allowlist of writable fields (mass assignment): anything else
    # in the JSON body is ignored, and both fields are bounded.
    status: str = Field(max_length=32)
    assignee: str = Field("", max_length=200)


# --------------------------------------------------------------------- #
# Read endpoints (reuse helpers that ALREADY exist -- zero new SQL)
# --------------------------------------------------------------------- #
@router.get("/results", response_model=list[ResultOut])
async def api_results(
    country: str = "", engine: str = "", device: str = "", classification: str = "",
    limit: int = 50, offset: int = 0, sid: int = Depends(api_subject),
):
    """Reuses Database.get_results_filtered (same helper as /results,
    2.2/1.1/1.3) -- no new SQL. `engine`/`device` are whitelisted against
    apify.actors.ACTORS/VALID_DEVICES, same defensive approach as
    dashboard.main.show_results: a value outside the catalog is ignored
    instead of interpolated. `offset`: get_results_filtered doesn't support
    it (LIMIT without OFFSET) -- limit+offset is requested and sliced in
    Python instead of touching that shared query (don't duplicate SQL,
    9.3 brief)."""
    if engine not in ACTORS:
        engine = ""
    if device not in VALID_DEVICES:
        device = ""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    rows = db.for_subject(sid).get_results_filtered(
        country_code=country, engine=engine, device=device,
        classification=classification, limit=limit + offset,
    )
    return _with_subject(rows[offset:offset + limit], sid)


@router.get("/runs", response_model=list[RunOut])
async def api_runs(limit: int = 50, sid: int = Depends(api_subject)):
    """Reuses Database.list_runs (same source as /runs)."""
    return _with_subject(db.for_subject(sid).list_runs(limit=_clamp_limit(limit)), sid)


@router.get("/alerts", response_model=list[AlertOut])
async def api_alerts(status: str = "active", limit: int = 100,
                      sid: int = Depends(api_subject)):
    """Reuses Database.list_alerts (same source as /alerts). `status` is
    not whitelisted here on purpose: list_alerts already falls back
    defensively to 'active' for any unknown value (see its docstring) --
    duplicating that validation here would only risk diverging from it."""
    return _with_subject(db.for_subject(sid).list_alerts(limit=_clamp_limit(limit), status=status), sid)


@router.get("/stats", response_model=StatsOut)
async def api_stats(engine: str = "google", sid: int = Depends(api_subject)):
    """Reuses Database.get_stats (same source as the home page /)."""
    if engine not in ACTORS:
        engine = "google"
    return _with_subject(db.for_subject(sid).get_stats(engine=engine), sid)



# --------------------------------------------------------------------- #
# Bounded write (C22): the only mutation in this router -- PATCH of an
# alert's STATE, reusing the 7.2 lifecycle as-is.
# --------------------------------------------------------------------- #
@router.patch("/alerts/{alert_id}", response_model=AlertOut)
async def api_patch_alert(
    alert_id: int, body: AlertStatusIn, request: Request,
    _auth: dict = Depends(require_scope("write:alerts")),
    subject: int | None = _SUBJECT_PARAM,
):
    """Marks an alert 'acknowledged' or 'resolved' (C22 acceptance #1: "an
    external flow... marks it acknowledged via PATCH with its API key").
    Requires scope write:alerts (403 if the key is valid but doesn't have
    it, see require_scope) -- does NOT reimplement the lifecycle: it
    delegates to Database.ack_alert/resolve_alert, the exact same helpers
    the /alerts web UI uses (7.2). Audited (C24, same approach as the rest
    of the repo's writes): actor_type='api_key' with the key's NAME (never
    its value -- see dashboard.main.audit, "NEVER pass secret values")."""
    if body.status not in _ALERT_PATCH_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {_ALERT_PATCH_STATUSES}",
        )
    sid = resolve_api_subject(_auth, subject)
    sdb = db.for_subject(sid)
    # An alert of another subject is "not found" (never "forbidden"): the
    # caller can't probe which ids exist elsewhere.
    existing = sdb.get_alert(alert_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="alert not found")

    with subject_scope(sid):
        if body.status == "acknowledged":
            sdb.ack_alert(alert_id, assignee=body.assignee)
        else:
            sdb.resolve_alert(alert_id, by=f"api_key:{_auth.get('name', '')}")

    from dashboard.main import (
        audit,  # late: avoids the import cycle when the module loads
    )
    audit(request, "api_alert_status_change", resource_type="alert", resource_id=alert_id,
          actor_type="api_key", actor_id=_auth.get("name", ""), status=body.status,
          subject_id=sid)
    return _with_subject(sdb.get_alert(alert_id), sid)
