# Evidence

Walkthrough tested: no. Written from the code; on 2026-10-30 a read-only
check opened it in the demo and found the selectors below.

Saved HTML copies of negative pages with a SHA-256 fingerprint taken at
capture time. New negative results are captured automatically after a run;
a row's ⋯ menu can save one by hand.

## How a user gets there

- Results toolbar → **More** → **Evidence** (`/ui/evidence`, side panel).
- A negative row's ⋯ menu → **Save evidence snapshot** (POST
  `/evidence/capture`).

## Driving it

```python
page.goto(f"{BASE}/results")
page.locator("[data-popover='#results-more']").click()
page.get_by_role("menuitem", name="Evidence").click()
panel = page.get_by_role("dialog", name=re.compile(r"^Evidence"))
panel.locator("#ev-url")                       # page picker, 2+ pages only
panel.locator("a[href^='/evidence/'][download]")
```

## Checks

- `evidence` rows: `url`, `url_hash`, `captured_at`, `html_path`,
  `sha256_html`, `trigger` (`auto_negative` or `manual`).
- The file at `html_path` exists under `<DB_PATH dir>/evidence/` and its
  SHA-256 equals `sha256_html`.
- A manual capture adds an `audit_log` row with `action='evidence_capture'`.

## Traps

- Manual capture fetches the real page. Demo URLs are `.example`, so it fails
  on purpose with "demo addresses (.example) don't exist on the internet".
  Check the panel with the 3 seeded snapshots instead.
