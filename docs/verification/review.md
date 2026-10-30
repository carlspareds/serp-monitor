# Review

Walkthrough tested: yes, passed 6 of 6 checks on 2026-10-29.

One uncertain result at a time ("1 of 2", progress bar): Positive /
Negative / Neutral / Not this person (keys 1-4) and Skip. A verdict sets
`classified_by='human'` and `confidence=1.0`; rules and AI never overwrite it.

## How a user gets there

- Results toolbar → **Review N** (`/ui/review`), the queue.
- A row's ⋯ menu → **Change label…** (`/ui/review?id=<result_id>`), one item.

## Driving it

```python
page.goto(f"{BASE}/results")
page.get_by_role("link", name=re.compile(r"^Review")).click()
dialog = page.get_by_role("dialog", name=re.compile(r"^Review"))
dialog.locator(".rev-count")          # "1 of 2"
dialog.locator("form.rev-form")       # action="/review/<id>/verdict"
dialog.get_by_role("button", name=re.compile(r"^Negative")).click()
```

## Checks

- `search_results` row: `classification='negative'`, `classified_by='human'`,
  `confidence=1.0`, `review_status` in (`approved`, `corrected`).
- One new `audit_log` row with `action='review_verdict'` and
  `resource_id=<url>`.
- Back on `/results`, `tr#r-<id>` has `data-label="negative"`.

## Traps

- The demo seed queues exactly 2 results. After the last verdict the dialog
  has no `form.rev-form`.
