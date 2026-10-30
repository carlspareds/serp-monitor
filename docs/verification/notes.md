# Notes

Walkthrough tested: no. Written from the code; on 2026-10-30 a read-only
check opened it in the demo and found the selectors below.

One note per URL: text, status (open, in progress, escalated, resolved,
dismissed), assignee, due date, and a history of status changes.

## How a user gets there

- A row's ⋯ menu → **Add or edit note** (`/ui/note?url=...`, side panel).
- Results toolbar → **More** → **Notes** (`/ui/notes`, all notes by status).

## Driving it

```python
page.goto(f"{BASE}/results")
page.locator("[data-popover^='#rm-']").first.click()
page.get_by_role("menuitem", name="Add or edit note").click()
panel = page.get_by_role("dialog", name=re.compile(r"^Note"))
panel.locator("#nn")      # textarea name="note"
panel.locator("#ns")      # select name="status"
panel.locator("#ndue")    # input type=date name="due_date"
panel.locator("#nas")     # assignee
panel.get_by_role("button", name="Save note").click()
```

## Checks

- `url_notes` row for the URL with the new text, assignee and due date.
- A status change adds a `note_history` row (`from_status`, `to_status`,
  `by`) and an `audit_log` row `note_status_change`; every save adds
  `note_save`.
- An overdue note shows the `overdue` tag.

## Traps

- The demo seed has 2 notes: `scam.example` (in progress, due in 14 days)
  and `reviews.example` (resolved).
- Saving an empty note on a URL that has one deletes it ("Note removed.").
