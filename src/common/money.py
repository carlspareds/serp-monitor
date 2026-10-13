"""How the app words an Apify cost estimate.

The per-run figure is the average `cost_usd` of the subject's recent runs.
Real Google/Bing runs can cost a fraction of a cent, so "about $0.00" was
shown -- never print that: below one cent it's "under $0.01 per country and
engine". The assistant's search card (static/app.js `estimate`) mirrors this.
"""

# Below this per-run average the estimate is worded "under $0.01 ...".
MIN_SHOWN_USD = 0.01
UNDER_A_CENT = "under $0.01 per country and engine"


def run_cost_phrase(per_run: float, runs: int = 1) -> str:
    """'about $0.12' for `runs` runs of `per_run` each, or UNDER_A_CENT when
    one run averages less than a cent (never 'about $0.00')."""
    per_run = max(float(per_run or 0), 0.0)
    if per_run < MIN_SHOWN_USD:
        return UNDER_A_CENT
    return f"about ${per_run * max(int(runs), 1):.2f}"
