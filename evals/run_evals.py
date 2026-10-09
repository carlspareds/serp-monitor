"""Evaluation harness -- real metrics computed against labeled synthetic
data, not fabricated numbers. Run with:

    .venv/bin/python evals/run_evals.py

Prints a human-readable report and writes evals/results.json.

What this measures (and why): see evals/README.md for the full
methodology, including which metrics this repo deliberately does NOT
report (scraping success rate and live end-to-end latency both require a
paid Apify API key and real network access, which don't ship with a
public demo repo -- reporting them here would mean either faking numbers
or silently running real paid API calls in a public CI, neither of which
is acceptable).
"""
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apify.actors import normalize_bing, normalize_google
from database.models import Database
from processing.alerts import generate_alerts
from processing.classifier import classify_batch, classify_result

# ========================================================================= #
# 1) SERP parsing accuracy: does the Apify dataset normalizer extract the
#    fields we actually rely on (url, position, title, domain) correctly
#    from the raw actor payload shape? Includes Bing's breadcrumb-vs-
#    redirector edge case (see apify.actors._bing_real_url).
# ========================================================================= #
_GOOGLE_CASES = [
    {
        "raw": [{
            "searchQuery": {"term": "Jordan Ashworth"},
            "organicResults": [
                {"position": 1, "title": "Jordan Ashworth",
                 "url": "https://jordanashworth.example/",
                 "displayedUrl": "jordanashworth.example",
                 "description": "Official site."},
                {"position": 2, "title": "Jordan's Blog",
                 "url": "https://jordan-ashworth.example/blog",
                 "displayedUrl": "jordan-ashworth.example",
                 "description": "Personal blog."},
            ],
        }],
        "country": "us",
        "expected": [
            {"position": 1, "url": "https://jordanashworth.example/",
             "domain": "jordanashworth.example"},
            {"position": 2, "url": "https://jordan-ashworth.example/blog",
             "domain": "jordan-ashworth.example"},
        ],
    },
]

_BING_CASES = [
    {
        # Normal case: displayedUrl is a clean breadcrumb.
        "raw": [{"organicResults": [
            {"title": "Jordan Ashworth", "url": "https://bing.com/ck/a?u=redirect1",
             "displayedUrl": "https://jordanashworth.example › about"},
        ]}],
        "country": "us",
        "expected": [{"url": "https://jordanashworth.example/about",
                       "domain": "jordanashworth.example"}],
    },
    {
        # Edge case: no usable displayedUrl -- must fall back to the raw
        # redirector URL rather than drop the row or raise.
        "raw": [{"organicResults": [
            {"title": "No breadcrumb", "url": "https://bing.com/ck/a?u=redirect2",
             "displayedUrl": ""},
        ]}],
        "country": "us",
        "expected": [{"url": "https://bing.com/ck/a?u=redirect2", "domain": "bing.com"}],
    },
]


def eval_serp_parsing() -> dict:
    total = 0
    correct = 0
    failures = []
    for case in _GOOGLE_CASES:
        got = normalize_google(case["raw"], case["country"])
        for expected, actual in zip(case["expected"], got, strict=True):
            total += 1
            ok = all(actual.get(k) == v for k, v in expected.items())
            correct += ok
            if not ok:
                failures.append({"engine": "google", "expected": expected, "actual": actual})
    for case in _BING_CASES:
        got = normalize_bing(case["raw"], case["country"])
        for expected, actual in zip(case["expected"], got, strict=True):
            total += 1
            ok = all(actual.get(k) == v for k, v in expected.items())
            correct += ok
            if not ok:
                failures.append({"engine": "bing", "expected": expected, "actual": actual})
    return {"metric": "serp_parsing_accuracy", "n_cases": total,
            "accuracy": correct / total if total else None, "failures": failures}


# ========================================================================= #
# 2) Classification precision/recall: rule-based classifier
#    (processing.classifier) against a synthetic test set. This is
#    the same engine used in production (no live AI classifier here --
#    Gemini is a separate, optional layer covered by tests/test_severity.py
#    and tests/test_ai_cache.py, not by this harness, since it requires a
#    paid API key).
# ========================================================================= #
# The fictional demo subject owned domains: per subject in the DB since multi-subject
# phase 1 (config/positive_domains.py ships an empty global list), so the synthetic
# cases pass them explicitly -- the same list scripts/seed_demo_data.py seeds.
_DEMO_POSITIVE_DOMAINS = ["jordanashworth.example", "jordan-ashworth.example",
                          "ashworthgroup.example"]

_CLASSIFICATION_CASES = [
    # (result, expected_label)
    ({"domain": "jordanashworth.example", "url": "https://jordanashworth.example/",
      "title": "Jordan Ashworth", "description": "Official site."}, "positive"),
    ({"domain": "jordan-ashworth.example", "url": "https://jordan-ashworth.example/blog",
      "title": "Jordan's Blog", "description": "Personal blog."}, "positive"),
    ({"domain": "ashworthgroup.example", "url": "https://ashworthgroup.example/about",
      "title": "Ashworth Group", "description": "Company page."}, "positive"),
    ({"domain": "scam.example", "url": "https://scam.example/article",
      "title": "Fraud lawsuit against Jordan Ashworth",
      "description": "Alleged fraud case."}, "negative"),
    ({"domain": "news.example", "url": "https://news.example/piece",
      "title": "Jordan Ashworth convicted", "description": "Court records."}, "negative"),
    ({"domain": "crime.example", "url": "https://crime.example/x",
      "title": "Ponzi scheme indictment", "description": "Federal charges filed."},
     "negative"),
    ({"domain": "gossip.example", "url": "https://gossip.example/x",
      "title": "Neighbor sued over fence", "description": "Local dispute."}, "negative"),
    ({"domain": "directory.example", "url": "https://directory.example/entry",
      "title": "Jordan Ashworth profile", "description": "Directory entry."}, "neutral"),
    ({"domain": "wiki.example", "url": "https://wiki.example/Jordan_Ashworth",
      "title": "Jordan Ashworth", "description": "Biography."}, "neutral"),
    ({"domain": "forum.example", "url": "https://forum.example/thread/1",
      "title": "Anyone know Jordan Ashworth?", "description": "Discussion thread."},
     "neutral"),
    # Exercises the real shipped FORCE_NEUTRAL_URL_FRAGMENTS entry (UK
    # Companies House): would otherwise classify as negative on the
    # "court" keyword, but a public-record source must never be flagged
    # negative just because a namesake has an unrelated court filing.
    ({"domain": "find-and-update.company-information.service.gov.uk",
      "url": "https://find-and-update.company-information.service.gov.uk/company/12345",
      "title": "Jordan Ashworth – court records archive",
      "description": "Public filing index."}, "neutral"),
]


def eval_classification() -> dict:
    labels = ["positive", "negative", "neutral"]
    tp = dict.fromkeys(labels, 0)
    fp = dict.fromkeys(labels, 0)
    fn = dict.fromkeys(labels, 0)
    correct = 0
    for result, expected in _CLASSIFICATION_CASES:
        predicted = classify_result(result, positive_domains=_DEMO_POSITIVE_DOMAINS)
        if predicted == expected:
            correct += 1
            tp[expected] += 1
        else:
            fp[predicted] += 1
            fn[expected] += 1

    per_class = {}
    for label in labels:
        precision = tp[label] / (tp[label] + fp[label]) if (tp[label] + fp[label]) else None
        recall = tp[label] / (tp[label] + fn[label]) if (tp[label] + fn[label]) else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else None)
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1}

    return {"metric": "classification_accuracy", "n_cases": len(_CLASSIFICATION_CASES),
            "accuracy": correct / len(_CLASSIFICATION_CASES), "per_class": per_class}


# ========================================================================= #
# 2b) Classification accuracy on REAL, independently-existing data: the
#     eval above is against synthetic cases invented for this repo, which a
#     sharp reviewer will (rightly) call circular -- the classifier's own
#     rules and the fixtures could be biased toward each other by
#     construction. This one runs the same classifier against
#     evals/real_labeled_data.py: 53 real search results for 6 well-known
#     public figures/companies (none of them clients, none of them the
#     classifier's test fixtures), labeled by an AI assistant reading each
#     result's actual title/URL, independently of the classifier's rules.
#     See evals/README.md for the full methodology and its limitations
#     (no exact SERP rank, single-evaluator labels, title-only).
# ========================================================================= #
def eval_real_classification() -> dict:
    from real_labeled_data import REAL_LABELED_RESULTS

    labels = ["positive", "negative", "neutral"]
    tp = dict.fromkeys(labels, 0)
    fp = dict.fromkeys(labels, 0)
    fn = dict.fromkeys(labels, 0)
    correct = 0
    misclassified = []
    for query, url, title, expected in REAL_LABELED_RESULTS:
        domain = url.split("/")[2].removeprefix("www.")
        result = {"domain": domain, "url": url, "title": title, "description": ""}
        predicted = classify_result(result)
        if predicted == expected:
            correct += 1
            tp[expected] += 1
        else:
            fp[predicted] += 1
            fn[expected] += 1
            misclassified.append({"url": url, "title": title,
                                   "expected": expected, "predicted": predicted})

    per_class = {}
    for label in labels:
        precision = tp[label] / (tp[label] + fp[label]) if (tp[label] + fp[label]) else None
        recall = tp[label] / (tp[label] + fn[label]) if (tp[label] + fn[label]) else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else None)
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1}

    n = len(REAL_LABELED_RESULTS)
    return {"metric": "real_data_classification_accuracy", "n_cases": n,
            "accuracy": correct / n, "per_class": per_class,
            "misclassified": misclassified}


# ========================================================================= #
# 3) Alert-rule correctness: given a controlled two-run history in a real
#    (temporary) database, does generate_alerts() fire exactly the alerts
#    it should -- no more, no less? severity_threshold's default is 5 and
#    an unscored negative counts as NULL_SEVERITY=3, so 1 negative (sum 3)
#    must NOT cross it and 2 negatives (sum 6) must.
# ========================================================================= #
def _make_result(domain: str, position: int, classification: str) -> dict:
    return {"query": "Jordan Ashworth", "url": f"https://{domain}/", "title": domain,
            "description": "", "displayed_url": domain, "position": position,
            "country_code": "us", "domain": domain, "classification": classification}


def eval_alert_rules() -> dict:
    scenarios = []

    # Scenario A: single negative (severity sum 3 < default threshold 5) --
    # must NOT raise a threshold alert.
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(db_path=str(Path(tmp) / "a.db"))
        db.save_run("run-a1", ["Jordan Ashworth"], "us", "completed",
                    {"total": 1, "positive": 0, "negative": 1, "neutral": 0})
        db.save_results([_make_result("scam.example", 1, "negative")], run_id="run-a1")
        msgs = generate_alerts(db)
        fired = any("Threshold" in m or "threshold" in m for m in msgs)
        scenarios.append({"name": "single_negative_below_threshold",
                           "expected_threshold_alert": False,
                           "got_threshold_alert": fired, "pass": fired is False})

    # Scenario B: two negatives (severity sum 6 >= 5) -- MUST raise a
    # threshold alert.
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(db_path=str(Path(tmp) / "b.db"))
        db.save_run("run-b1", ["Jordan Ashworth"], "us", "completed",
                    {"total": 2, "positive": 0, "negative": 2, "neutral": 0})
        db.save_results([
            _make_result("scam.example", 1, "negative"),
            _make_result("news.example", 2, "negative"),
        ], run_id="run-b1")
        msgs = generate_alerts(db)
        fired = any("over the" in m.lower() or "threshold" in m.lower() for m in msgs)
        scenarios.append({"name": "two_negatives_cross_threshold",
                           "expected_threshold_alert": True,
                           "got_threshold_alert": fired, "pass": fired is True})

    # Scenario C: a brand-new negative that wasn't negative in the
    # previous run -- MUST raise a negative_new alert.
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(db_path=str(Path(tmp) / "c.db"))
        db.save_run("run-c1", ["Jordan Ashworth"], "us", "completed",
                    {"total": 1, "positive": 1, "negative": 0, "neutral": 0})
        db.save_results([_make_result("jordanashworth.example", 1, "positive")],
                        run_id="run-c1")
        with db._connect() as conn:
            conn.execute("UPDATE search_results SET scraped_at = '2026-01-01T09:00:00Z' "
                         "WHERE run_id = 'run-c1'")
            conn.commit()
        db.save_run("run-c2", ["Jordan Ashworth"], "us", "completed",
                    {"total": 2, "positive": 1, "negative": 1, "neutral": 0})
        db.save_results([
            _make_result("jordanashworth.example", 1, "positive"),
            _make_result("scam.example", 2, "negative"),
        ], run_id="run-c2")
        with db._connect() as conn:
            conn.execute("UPDATE search_results SET scraped_at = '2026-01-08T09:00:00Z' "
                         "WHERE run_id = 'run-c2'")
            conn.commit()
        msgs = generate_alerts(db)
        fired = any("new negative" in m.lower() for m in msgs)
        scenarios.append({"name": "brand_new_negative_vs_previous_run",
                           "expected_negative_new_alert": True,
                           "got_negative_new_alert": fired, "pass": fired is True})

    n_pass = sum(1 for s in scenarios if s["pass"])
    return {"metric": "alert_rule_correctness", "n_scenarios": len(scenarios),
            "pass_rate": n_pass / len(scenarios), "scenarios": scenarios}


# ========================================================================= #
# 4) Processing latency: classify_batch() + Database.save_results() for a
#    realistic batch size, repeated to report mean/p95. This is OUR
#    processing time only -- it excludes Apify's own scrape time (roughly
#    20-90s per country, dominated by a third-party actor we don't control and
#    can't reproduce without a paid API key -- see evals/README.md).
# ========================================================================= #
def eval_processing_latency(batch_size: int = 20, repeats: int = 15) -> dict:
    durations_ms = []
    for i in range(repeats):
        results = [_make_result(f"site-{j}.example", j + 1,
                                "neutral") for j in range(batch_size)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(db_path=str(Path(tmp) / "lat.db"))
            run_id = f"run-lat-{i}"
            db.save_run(run_id, ["Jordan Ashworth"], "us", "completed",
                        {"total": batch_size, "positive": 0, "negative": 0,
                         "neutral": batch_size})
            start = time.perf_counter()
            classify_batch(results)
            db.save_results(results, run_id=run_id)
            durations_ms.append((time.perf_counter() - start) * 1000)

    durations_ms.sort()
    p95_index = min(len(durations_ms) - 1, int(len(durations_ms) * 0.95))
    return {"metric": "processing_latency_ms", "batch_size": batch_size,
            "repeats": repeats, "mean_ms": round(statistics.mean(durations_ms), 2),
            "p95_ms": round(durations_ms[p95_index], 2),
            "note": "classify_batch + save_results only -- excludes Apify scrape time"}


def main() -> None:
    results = {
        "serp_parsing": eval_serp_parsing(),
        "classification": eval_classification(),
        "real_data_classification": eval_real_classification(),
        "alert_rules": eval_alert_rules(),
        "processing_latency": eval_processing_latency(),
        "not_computed": {
            "scraping_success_rate": (
                "Requires a live, paid Apify API key and real network access. "
                "Not run here (no credentials ship with this repo, and it "
                "would mean making real paid API calls from a public CI). "
                "Run scripts/smoke_apify.py with your own APIFY_API_KEY to "
                "check this against a live actor run."
            ),
            "end_to_end_search_latency": (
                "The dominant cost of a real search is Apify's own actor "
                "execution time (the app's own estimate is ~20-90s per "
                "country), which this "
                "repo has no way to reproduce honestly without live "
                "credentials. See processing_latency above for what IS "
                "measured: our own code's processing time given already-"
                "fetched results."
            ),
        },
    }

    out_path = Path(__file__).parent / "results.json"
    out_path.write_text(json.dumps(results, indent=2))

    print("=== SERP Monitor evals ===\n")
    sp = results["serp_parsing"]
    print(f"SERP parsing accuracy: {sp['accuracy']:.0%} ({sp['n_cases']} cases)")

    cl = results["classification"]
    print(f"Classification accuracy: {cl['accuracy']:.0%} ({cl['n_cases']} cases)")
    for label, m in cl["per_class"].items():
        p = f"{m['precision']:.0%}" if m["precision"] is not None else "n/a"
        r = f"{m['recall']:.0%}" if m["recall"] is not None else "n/a"
        print(f"  {label:10s} precision={p:>5s} recall={r:>5s}")

    rd = results["real_data_classification"]
    print(f"Classification accuracy (REAL data, {rd['n_cases']} cases, "
          f"6 public figures/companies): {rd['accuracy']:.0%}")
    for label, m in rd["per_class"].items():
        p = f"{m['precision']:.0%}" if m["precision"] is not None else "n/a"
        r = f"{m['recall']:.0%}" if m["recall"] is not None else "n/a"
        print(f"  {label:10s} precision={p:>5s} recall={r:>5s}")
    if rd["misclassified"]:
        print(f"  {len(rd['misclassified'])} misclassified (see results.json for detail)")

    ar = results["alert_rules"]
    print(f"Alert-rule correctness: {ar['pass_rate']:.0%} "
          f"({sum(s['pass'] for s in ar['scenarios'])}/{ar['n_scenarios']} scenarios)")

    pl = results["processing_latency"]
    print(f"Processing latency (batch={pl['batch_size']}): "
          f"mean={pl['mean_ms']}ms p95={pl['p95_ms']}ms")

    print("\nNot computed (requires live Apify credentials -- see evals/README.md):")
    for name in results["not_computed"]:
        print(f"  - {name}")

    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
