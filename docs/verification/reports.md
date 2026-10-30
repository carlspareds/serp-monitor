# Reports

Walkthrough tested: no. Written from the code; on 2026-10-30 a read-only
check opened it in the demo and found the selectors below.

## How a user gets there

Results toolbar → **More** → **Reports** (`/ui/reports`, dialog). Two tabs
(links with `aria-pressed`): **New report** and **Previous reports**.

## Driving it

```python
page.goto(f"{BASE}/results")
page.locator("[data-popover='#results-more']").click()
page.get_by_role("menuitem", name="Reports").click()
dialog = page.get_by_role("dialog", name=re.compile(r"^Reports"))
dialog.get_by_role("link", name=re.compile(r"^Use in scripts"))  # CSV tile
dialog.get_by_role("button", name="Create CSV").click()
with page.expect_download() as dl:
    dialog.get_by_role("link", name="Download").click()
```

The kind is a tile, not a radio button: "Share with a client or manager"
(PDF, button "Create PDF"), "Analyse in a spreadsheet" (Excel, "Create
workbook"), "Use in scripts or other tools" (CSV, "Create CSV").

## Checks

- A new file and its JSON sidecar in `<DB_PATH dir>/exports/<subject_id>/`.
  There is no `reports` table.
- One `audit_log` row with `action='report_create_<kind>'` and
  `resource_type='report'`.
- The file is listed under Previous reports.

## Traps

- Picking a tile reloads the dialog body: wait for the Create button
  (`wait_for()`) before counting or clicking it.
- After Create, the button is replaced by Download in the same dialog.
- Creating a report is a write (admin or operator once accounts exist); the
  no-account demo allows writes.
