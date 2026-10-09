"""Diff between any two runs (4.2): entered/exited/moved/reclassified.

`run_diff` is the ONLY source of diffs in the system (ponytail): the /diff
page, the "diff vs previous" link on /runs and the changes block of the
weekly report (report/weekly.py) all go through here -- see also
report.weekly._detect_new_negatives, rewritten on top of this function.
"""
from database.models import url_hash

_NO_LIMIT = 100_000  # practical ceiling for "no limit" in get_results_filtered


def _best_position_map(results: list[dict]) -> dict:
    """Map url_hash -> result of a SINGLE run, keeping the BEST position if
    the URL appears more than once (C5 -- e.g. 2 queries that return the
    same URL with different classifications): sorts by position ascending
    (None last) and keeps the first occurrence of each hash, which after
    the sort is already the best-positioned one."""
    ordered = sorted(results, key=lambda r: (r.get("position") is None, r.get("position")))
    out: dict = {}
    for r in ordered:
        h = url_hash(r.get("url") or "")
        if h not in out:
            out[h] = r
    return out


def run_diff(db, run_a: str, run_b: str) -> dict:
    """Diff between run_a (old) and run_b (new).

    Operates on `db.get_results_filtered(run_id=...)` -- excludes
    same_person='no' by default (2.3) -- so a namesake never appears in any
    diff block, same as the rest of the system. Row identity = url_hash
    (normalized URL, 2.5): two equivalent URLs (trailing slash, utm_*...)
    count as the SAME row and don't generate a false entered/exited. If the
    same URL appears more than once in the SAME run (e.g. 2 queries), the
    one with the best position is kept (C5, see _best_position_map).

    Contract (C9): delta = position_a - position_b; positive = moved up
    (improved rank). 'moved' is only reported when BOTH positions are
    integers -- a transition from/to position=None doesn't count as a move
    (C6). 'moved' and 'reclassified' are not mutually exclusive (C8): the
    same row can change position AND classification at once, and appears
    in both blocks.

    run_a or run_b empty/falsy -> empty diff without touching the DB (C7).

    Returns:
      entered: results present in B but not in A (list[dict])
      exited: results present in A but not in B (list[dict])
      moved: [(result_b, delta)]
      reclassified: [(result_b, classification_in_a, classification_in_b)]
      hidden_same_name: how many pages were left out of both sides because a
        row of them is marked same_person='no' in either run
    """
    if not run_a or not run_b:
        return {"entered": [], "exited": [], "moved": [], "reclassified": [], "hidden_same_name": 0}

    # Namesakes are excluded from BOTH sides consistently (final live test): a
    # page marked "not this person" in either run is hidden in both -- else a
    # page hidden only in the newer run showed up as "left".
    results_a = db.get_results_filtered(run_id=run_a, limit=_NO_LIMIT, include_homonyms=True)
    results_b = db.get_results_filtered(run_id=run_b, limit=_NO_LIMIT, include_homonyms=True)
    hidden = {url_hash(r.get("url") or "") for r in (*results_a, *results_b)
              if r.get("same_person") == "no"}
    map_a = {h: r for h, r in _best_position_map(results_a).items() if h not in hidden}
    map_b = {h: r for h, r in _best_position_map(results_b).items() if h not in hidden}

    entered = [map_b[h] for h in map_b if h not in map_a]
    exited = [map_a[h] for h in map_a if h not in map_b]

    moved = []
    reclassified = []
    for h, ra in map_a.items():
        rb = map_b.get(h)
        if rb is None:
            continue
        pa, pb = ra.get("position"), rb.get("position")
        if isinstance(pa, int) and isinstance(pb, int) and pa != pb:
            moved.append((rb, pa - pb))
        cls_a, cls_b = ra.get("classification"), rb.get("classification")
        if cls_a != cls_b:
            reclassified.append((rb, cls_a, cls_b))

    return {"entered": entered, "exited": exited, "moved": moved, "reclassified": reclassified,
            "hidden_same_name": len(hidden)}
