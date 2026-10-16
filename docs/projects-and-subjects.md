# Projects and subjects

How one install watches several names or clients: subjects, projects,
roles, what the scheduled jobs do per subject, and how the API scopes to
a subject.

[Back to README](../README.md)

One install can watch several people or brands. A **subject** is one tracked
name (its search term, aliases, countries, context for the AI prompts, and
its own positive/negative domains); every run, result, alert, note, piece of
evidence and report belongs to exactly one subject. A **project** groups
subjects (for example, one per client) and decides what a client viewer can
see.

- **Add a subject:** top bar → *Project ▸ Subject* switcher → *New subject*,
  or Settings → Projects (also where projects are created, renamed and
  archived). Archiving a project deactivates its subjects; their data is
  kept. Subject 1 is the default one and can't be deactivated.
- **Default projects:** a fresh install has one project, *My project*
  (installs from before this release still named *Internal* are renamed
  once at startup; a name you chose is kept). The first subject you add in
  the setup wizard goes there. The demo seed puts its fictional subject in
  a separate *Demo* project (created if missing, never by renaming one).
- **Reports → CSV** files are named `<project>-<subject>-results-<date>.csv`
  and hold only the countries picked in the dialog (header row + data, no
  comment lines).
- **Switching** changes the subject every page, dialog, export and the
  assistant work on (an httponly `subject` cookie, re-checked on every
  request).
- **Roles:** admins and operators see every active subject. A *Viewer
  (read-only)* user tied to a project only sees that project's subjects and
  can't change anything. The three roles themselves (`admin`, `operator`,
  Viewer) and how accounts are added are in
  [`configuration.md`](configuration.md#accounts-and-roles).

**Scheduled jobs and alerts.** The weekly report and the daily alert check
run once per *scheduled* subject: active, not a demo subject, in a project
that isn't archived. Each subject gets its own searches (its query and
countries), its own Excel workbook under `data/exports/<subject_id>/` (listed
in Reports → Previous reports, marked "sent by email" once emailed) and its
own weekly email. Demo subjects are left out of everything scheduled: no paid
searches, no alert checks, no digest lines. One subject failing doesn't stop
the others; the job is marked as failed afterwards. The daily info digest is
**one** email per day with a section per subject. Retention purges, the Apify
budget check and the missed-job watchdog are deployment-wide.

Every notification (email, Telegram, Slack, ntfy) starts with
`<Project> ▸ <Subject>: `, except the two deployment-wide alerts (Apify
budget, missed job). Alerts are deduplicated per subject: the same condition
on two subjects gives two alerts. The Apify budget alert is the exception:
the budget covers the whole deployment (one Apify account), so it fires at
most **once a month**, whichever subject's search crossed it, and is stored
under subject 1. It shows in every subject's alerts popover while it is open
(read-only outside subject 1) and in Settings → Search for the rest of the
month. "Check now" in Settings → Notifications runs the alert checks for the
active subject only (the button names it); the daily job covers every subject.

Report files written before reports were stored per subject (flat
`data/exports/weekly_*.xlsx` and the old Export page's files) are moved to
subject 1's folder at startup and by the weekly job.

**API.** Every `/api/v1` endpoint takes an optional `subject` query parameter
(a subject id) and every row it returns carries `subject_id`. When creating
a key (Settings → API keys) you choose *All subjects* or one subject:

- a key limited to one subject always reads (and, with `write:alerts`,
  updates) that subject; asking for another one with `?subject=` is a 403;
- an *All subjects* key reads the subject named by `?subject=`, and subject 1
  when it's left out, so integrations written before subjects existed keep
  working unchanged even after more subjects are added;
- an unknown or inactive subject is a 404, and an alert of another subject is
  "not found" for `PATCH /api/v1/alerts/{id}`.

The row-level rules that enforce all of this server-side are
[`../SECURITY.md`](../SECURITY.md), item 4.
