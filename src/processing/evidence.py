"""
Evidence vault (3.2)
================================
HTML snapshot with hash and timestamp of a URL, stored in `evidence` (see
database.models._init_db). Two trigger modes:

  - Automatic: `capture_new_negatives(db, run_id)`, called at the same 2
    post-run chokepoints as processing.enrich.annotate_same_person /
    processing.lifecycle.update_url_lifecycle (report/multi_country.py::
    persist_engine_run) -- ALWAYS after update_url_lifecycle, so it reads
    the run's already-persisted same_person.
  - Manual: "Evidence" button in /results (dashboard.routers.evidence),
    trigger='manual'.

PLATFORM ADAPTATION: local PNG via Playwright/Chromium was DROPPED --
spinning up a headless Chromium per capture isn't viable on low-memory
hosts.
This module captures ONLY the raw HTML; the png_path/sha256_png columns
of `evidence` exist in the schema and stay NULL.

Total best-effort (same spirit as the other post-run steps): a network/IO
failure must NEVER break the run that triggers it -- capture_url returns
None and logs, it never raises.

Anti-SSRF (FIX 2, hardening audit): `capture_url` fetches `url`, which
comes from third-party data -- SERP results (auto) or a form (manual,
dashboard.routers.evidence) -- WITHOUT validating the destination before
this fix (`requests.get(..., allow_redirects=True)` followed redirects
without checking where they pointed, and the SSLError fallback called with
unconditional `verify=False`). Uses common.net.safe_urlopen, the shared
anti-SSRF guard. safe_urlopen validates scheme + private/loopback/link-local/CGNAT
IP, re-validated on EVERY redirect, and enforces the size cap DURING the
download (`resp.read(max_bytes)`, not after downloading it whole like
`resp.content` did) -- this also closes the memory hole on low-memory hosts
that a malicious server could exploit by serving a giant body with a fake
or absent Content-Length.

Arbitration (SSL fallback removed): the previous version retried ONCE with
`verify=False` if the first attempt failed with SSLError -- safe_urlopen
doesn't distinguish that case from any other network failure (everything
collapses to `(None, b"")`, best-effort), so retrying "insecure" on every
failure would be double work without discriminating SSL from DNS/timeout/
guard-blocked. The fallback is removed: a site with an invalid certificate
is now treated like any other network failure -- it's not captured, it's
logged, the cycle continues (the SAME best-effort contract as always). No
`verify=False` remains in this module.
"""
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from common.net import safe_urlopen
from database.models import url_hash as _url_hash

_UA = {"User-Agent": "Mozilla/5.0 (compatible; SEODashboard/1.0; +evidence-vault)"}
_TIMEOUT = 20
# Hard size ceiling per snapshot (# ponytail: 5MB comfortably covers a real
# HTML page; if more is ever needed, bump this constant).
# FIX 2 (hardening audit): now enforced by safe_urlopen DURING the download
# (resp.read(max_bytes)), not a later slice over the already-complete body
# -- see the module docstring.
_MAX_BYTES = 5 * 1024 * 1024
# Cap on automatic captures per call to capture_new_negatives (# ponytail:
# belt-and-suspenders over "no prior evidence" -- avoids a storm of
# outgoing requests if the first run after a deploy brings many historical
# negatives without evidence yet).
AUTO_CAPTURE_LIMIT = 10


def _now_iso() -> str:
    """Naive-UTC ISO -- same pattern as processing.lifecycle._now_iso (H7,
    arbitration 3.1): datetime.now(UTC).replace(tzinfo=None), not utcnow()
    (ruff DTZ003 -- database/models.py has the only free pass for being
    pre-existing code; new code doesn't)."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def evidence_dir(db) -> Path:
    """Root of the evidence vault: sibling of the DB (derived from
    db.db_path) -- resolves to data/evidence/ in a default install, to
    <tmp_path>/evidence/ in tests. Single source of truth for this path."""
    return Path(db.db_path).parent / "evidence"


def capture_url(db, url: str, trigger: str, result_id: int | None = None) -> dict | None:
    """Captures a best-effort HTML snapshot of `url` and records it in
    `evidence`. Returns the inserted row (dict) or None if something failed
    -- NEVER raises (best-effort, same contract as the other post-run
    steps).

    Network pattern (FIX 2, hardening audit -- see the module docstring):
    common.net.safe_urlopen, realistic UA, timeout 20, _MAX_BYTES cap
    applied DURING the download. The raw HTML (bytes, as-is) is stored at
    data/evidence/{yyyy}/{mm}/{sha256[:16]}.html -- the full sha256 of the
    STORED CONTENT (already truncated if applicable) is recorded in
    sha256_html; a filename collision is practically impossible.

    Size limit (# ponytail, FIX 2): a body >= _MAX_BYTES is annotated as
    "truncated" in `notes` -- a huge page must not fill up the host's
    disk. `status is None` covers both "blocked by the anti-SSRF guard"
    (private/loopback/CGNAT destination) and any real network failure
    (DNS, timeout, connection refused, broken TLS) -- best-effort, the
    reason isn't distinguished (see the module docstring's arbitration)."""
    try:
        status, body = safe_urlopen(url, timeout=_TIMEOUT, max_bytes=_MAX_BYTES, headers=_UA)
        if status is None:
            print(f"[evidence] capture blocked or failed for {url} "
                  "(anti-SSRF guard or network failure)")
            return None
        if status >= 400 or not body:
            print(f"[evidence] capture failed for {url}: HTTP {status}")
            return None

        notes = (f"HTML truncated to {_MAX_BYTES} bytes (capture limit)"
                 if len(body) >= _MAX_BYTES else "")

        sha256_html = hashlib.sha256(body).hexdigest()
        captured_at = _now_iso()

        subdir = evidence_dir(db) / captured_at[:4] / captured_at[5:7]
        subdir.mkdir(parents=True, exist_ok=True)
        html_path = subdir / f"{sha256_html[:16]}.html"
        html_path.write_bytes(body)

        h = _url_hash(url)
        evidence_id = db.add_evidence(
            url=url, url_hash=h, captured_at=captured_at, html_path=str(html_path),
            sha256_html=sha256_html, trigger=trigger, result_id=result_id, notes=notes,
        )
        return {
            "id": evidence_id, "url": url, "url_hash": h, "captured_at": captured_at,
            "html_path": str(html_path), "png_path": None, "sha256_html": sha256_html,
            "sha256_png": None, "trigger": trigger, "result_id": result_id, "notes": notes,
        }
    except Exception as exc:  # noqa: BLE001 -- best-effort, see module docstring
        print(f"[evidence] capture failed for {url}: {exc}")
        return None


def capture_new_negatives(db, run_id: str) -> list[dict]:
    """Automatic post-run trigger: for each 'negative' result of this run
    that is NOT a discarded namesake (same_person != 'no') and has NO prior
    evidence for that URL (has_evidence by url_hash), captures a snapshot
    with trigger='auto_negative'. Returns the list of successfully captured
    rows (best-effort: an individual failed capture simply doesn't appear,
    it doesn't interrupt the others).

    AUTO_CAPTURE_LIMIT cap per call -- "no prior evidence" already bounds
    the normal volume (a URL is only ever captured once in its lifetime),
    the limit is an extra belt. Total best-effort: any failure reading the
    run (DB) is logged and returns an empty list, never raises -- a run
    that's already been paid for and persisted must not fail because of
    this."""
    captured: list[dict] = []
    try:
        results = db.get_results_by_run(run_id)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        print(f"[evidence] capture_new_negatives: could not read run {run_id}: {exc}")
        return captured

    for r in results:
        if len(captured) >= AUTO_CAPTURE_LIMIT:
            break
        if r.get("classification") != "negative":
            continue
        if r.get("same_person") == "no":
            continue
        url = r.get("url") or ""
        h = _url_hash(url)
        if not h:
            continue
        try:
            already = db.has_evidence(h)
        except Exception as exc:  # noqa: BLE001 -- see docstring
            print(f"[evidence] capture_new_negatives: has_evidence failed for {url}: {exc}")
            continue
        if already:
            continue
        row = capture_url(db, url, trigger="auto_negative", result_id=r.get("id"))
        if row:
            captured.append(row)
    return captured
