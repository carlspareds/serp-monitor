# Compare

Walkthrough tested: no. Written from the code; on 2026-10-30 a read-only
check opened it in the demo and found the selectors below.

Older run A against newer run B of one country: entered, left, moved and
label changes. Read-only: it writes nothing to the database.

## How a user gets there

Results toolbar → **Compare** (`/ui/compare`, dialog titled "Compare runs").
With no runs picked it opens on the last two runs of the latest country.

## Driving it

```python
page.goto(f"{BASE}/results")
page.get_by_role("link", name="Compare").click()
dialog = page.get_by_role("dialog", name=re.compile(r"^Compare runs"))
dialog.locator("#cmp-country")   # Country
dialog.locator("#cmp-a")         # Older run (A)
dialog.locator("#cmp-b")         # Newer run (B)
```

The form submits itself when a select changes (`data-autosubmit`); there is
no Compare button except in `<noscript>`.

## Checks

- The Entered / Left / Moved counters match `run_diff` for the same two runs.
- Picking a newer run as A shows "The older run is always A, so the two were
  swapped."
- `audit_log` and `search_results` are unchanged.

## Traps

- One run of a country: "a second one is needed to compare".
- Two runs of different countries are refused with a message.
- No differences: "Nothing changed."
