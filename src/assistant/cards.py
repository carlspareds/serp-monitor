"""Structured reply cards for the assistant panel.

The harness turns the RESULTS of the tools it ran (never the model's free
text) into small card events the panel renders next to the plain answer:

  {"type": "card", "kind": "results" | "changes" | "review" | "report" | "sov",
   "tool": "<tool name>", "data": {...}}

  results  explain_result (one item, with its "why"), list_review_queue for
           viewers, list_results (the latest run's results, up to 10 rows
           with position, country and who set the label): title, domain,
           label, Open (the page) / Review (the Change label dialog) buttons.
  changes  list_changes: counts, the top entered/left/moved rows and the
           compare deep link (+ run ids) for "Open compare".
  review   list_review_queue for operators/admins: up to 3 pending items
           with the four verdict buttons, which POST the same audited
           endpoint as the Review dialog (/review/<id>/verdict,
           /review/<id>/not-this-person).
  report   list_reports (previous files) and a confirmed create_report
           (the new file): name, kind and a Download link.
  sov      get_overview: positive / negative / neutral of the latest run per
           country (a mini share-of-voice bar).

Everything in `data` comes from the tool result and is JSON-safe; the panel
escapes every string. URLs are same-site paths, except result URLs, which the
panel only puts in an <a target=_blank rel=noopener> after checking http(s).
"""
from __future__ import annotations

from assistant import tools

MAX_CARDS_PER_TURN = 4
_MAX_ROWS = 5
_REVIEW_ITEMS = 3
_RESULT_ROWS = 10


def _item(r: dict, why: str = "") -> dict:
    out = {"result_id": r.get("result_id"), "title": r.get("title") or r.get("url") or "",
           "domain": r.get("domain") or "", "url": r.get("url") or "",
           "label": r.get("label") or "neutral", "review_status": r.get("review_status") or ""}
    if why:
        out["why"] = tools._short(why, 240)
    return out


def _results_list(result: dict, ctx: tools.ToolContext) -> dict | None:
    rows = result.get("items") or []
    if not rows:
        return None
    items = []
    for r in rows[:_RESULT_ROWS]:
        it = _item(r)
        it.update(position=r.get("position"), country=r.get("country") or "",
                  source=r.get("source") or "", same_person=r.get("same_person") or "")
        items.append(it)
    what = "" if result.get("label") in (None, "", "all") else f"{result['label']} "
    where = "" if result.get("country") in (None, "", "all") else f" · {result['country']}"
    total = int(result.get("total") or len(rows))
    return {"title": f"Latest run · {total} {what}result{'' if total == 1 else 's'}{where}",
            "items": items, "more": max(0, total - len(items)),
            "can_review": tools.role_allows(ctx.role, "operator")}


def _sov(result: dict) -> dict | None:
    rows = []
    for c in result.get("latest_by_country") or []:
        run = c.get("latest_run") or {}
        total = int(run.get("total") or 0)
        if not total:
            continue
        rows.append({"country": c.get("country"), "engine": c.get("engine"), "date": run.get("date"),
                     "positive": int(run.get("positive") or 0), "negative": int(run.get("negative") or 0),
                     "neutral": int(run.get("neutral") or 0), "total": total})
    return {"rows": rows[:3]} if rows else None


def _changes(result: dict) -> dict:
    items = []
    for r in result.get("entered") or []:
        items.append({"kind": "entered", "domain": r.get("domain"), "position": r.get("position"),
                      "label": r.get("label"), "result_id": r.get("result_id")})
    for r in result.get("exited") or []:
        items.append({"kind": "exited", "domain": r.get("domain"), "position": r.get("position"),
                      "label": r.get("label")})
    for r in result.get("moved") or []:
        items.append({"kind": "moved", "domain": r.get("domain"), "position": r.get("position"),
                      "label": r.get("label"), "places": r.get("places")})
    a, b = result.get("run_a") or {}, result.get("run_b") or {}
    return {"country": result.get("country"), "engine": result.get("engine"),
            "from": tools.fmt_day(a.get("date")), "to": tools.fmt_day(b.get("date")),
            "summary": result.get("summary") or {}, "items": items[:_MAX_ROWS],
            "more": max(0, len(items) - _MAX_ROWS),
            "compare_url": result.get("compare_url") or tools.compare_url(),
            "params": {"run_a": a.get("run_id"), "run_b": b.get("run_id")}}


def _report_files(result: dict) -> dict | None:
    files = [{"file": f.get("name"), "kind": f.get("kind"), "title": f.get("title") or f.get("name"),
              "created": tools.fmt_day(f.get("created")), "url": f.get("download_url")}
             for f in result.get("previous_files") or [] if f.get("download_url")]
    return {"files": files[:_MAX_ROWS]} if files else None


def from_tool(name: str, result: dict, ctx: tools.ToolContext) -> dict | None:
    """The card for one read tool's result, or None."""
    if not isinstance(result, dict):
        return None
    kind, data = None, None
    if name == "get_overview":
        kind, data = "sov", _sov(result)
    elif name == "list_changes":
        kind, data = "changes", _changes(result)
    elif name == "explain_result" and result.get("result"):
        kind = "results"
        data = {"items": [_item(result["result"], result.get("why") or "")],
                "can_review": tools.role_allows(ctx.role, "operator")}
    elif name == "list_review_queue":
        items = [_item(r, r.get("why") or "") for r in result.get("items") or []]
        if not items:
            return None
        if tools.role_allows(ctx.role, "operator"):
            kind = "review"
            data = {"total": result.get("total_pending") or len(items), "items": items[:_REVIEW_ITEMS],
                    "review_url": tools.URL_REVIEW}
        else:
            kind, data = "results", {"items": items[:_MAX_ROWS], "can_review": False,
                                     "title": f"{result.get('total_pending') or len(items)} waiting for review"}
    elif name == "list_results":
        kind, data = "results", _results_list(result, ctx)
    elif name == "list_reports":
        kind, data = "report", _report_files(result)
    if not kind or not data:
        return None
    return {"type": "card", "kind": kind, "tool": name, "data": data}


def report_card(result: dict) -> dict | None:
    """Card for a confirmed create_report (exec_create_report's result)."""
    if not result.get("download_url"):
        return None
    name = result.get("file") or ""
    return {"type": "card", "kind": "report", "tool": "create_report",
            "data": {"files": [{"file": name, "kind": name.rsplit(".", 1)[-1], "title": name,
                                "created": "just now", "url": result["download_url"]}],
                     "fresh": True}}
