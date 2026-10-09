# UI architecture

Server-rendered FastAPI + Jinja2/f-strings, one small vanilla JS file
(`static/app.js`), one stylesheet (`static/style.css`), fonts bundled in
`static/fonts/`. No build step, no external requests.

## Shell

Every signed-in page is rendered by `ui_helpers.page()` (f-string pages) or
extends `templates/base.html` (Jinja pages); both use `ui_helpers.shell()`:

- **Top bar** (`nav.render_topbar`): brand, *Project ▸ Subject* switcher
  (popover `/ui/switcher`), "Find in stored results" (`/search-text`),
  Assistant button (`data-assistant-toggle`: `SM.assistant`, below; the
  fragment `/assistant/panel` is loaded into `<aside id="assistant-panel">`),
  background-search chip (`#jobchip`, `nav.render_jobs_chip`; see *Background
  searches* below), alerts bell (popover `/ui/alerts`), account menu
  (`<template id="acct-menu">`).
- **Rail** (`nav.render_rail`): exactly three items, Overview `/`, Results
  `/results`, Settings `/settings`. On phones (≤640px) it becomes a bottom tab
  bar. Pages still reachable by URL highlight the item they belong to
  (`nav.resolve`).
- `page(title, body, active, sub=..., actions=..., head=True)` renders the
  mockup page header (h1, muted subtitle, toolbar buttons).

## Active subject and viewer

`dashboard.context.SubjectMiddleware` (pure ASGI, outermost) sets per request:

- the **viewer** (`context.viewer()`: username, role, admin/write flags,
  allowed subjects). Staff see every active subject; a client viewer only the
  subjects of their project (`users.client_id`).
- the **active subject** from the `subject` cookie (httponly, SameSite=Lax),
  validated against the allowed subjects, falling back to the first one (id 0,
  no data, when nothing is allowed). It is set on the database ContextVar, so
  every `db.*` call in the request is scoped to it. `GET /subject/switch?id=&next=`
  changes it.
- the **flash** message (see PRG below).

## Overlays: the `SM` JS API

```js
SM.openModal(src, {wide, title})      // centred; bottom sheet on phones
SM.openPanel(src, {target, title})    // right side panel; target = persistent element id
SM.popover(anchor, src, {className})  // anchored menu
SM.close([el]); SM.closeAll(); SM.reload([el])
SM.toast(message, 'ok' | 'error' | 'info', {action, onAction})  // optional action button
SM.load(url)                          // fetch a fragment (sends X-SM-Fragment: 1)
SM.jobs.refresh()                     // re-read GET /jobs/active (top-bar chip)
```

`src` is a URL, an HTML string (`"<..."`) or a template id (`"#id"`). Focus is
moved in and trapped, Escape/backdrop close, focus returns to the opener
(when the opener is gone or hidden -- e.g. it lives in the minimized
assistant -- to the overlay below, the docked assistant, or `#main`), body
scroll is locked for modals/panels. Fragment `<script>`s run after insertion and
an `sm:load` event bubbles from the overlay.

### Stacking

SM keeps ONE stack of modals/panels. `restack()` gives each entry a z-index
from its depth: scrim `80 + 2·depth`, overlay `81 + 2·depth`
(`SM.zFor(depth)`), so the newest overlay is always topmost and owns focus,
the Tab trap and Escape; popovers get the next level up. `SM.top()` describes
the top entry (`{url, kind, title}`), `SM.depth()` its size. Every open/close
fires `sm:open` / `sm:close` on `document` (`detail: {kind, url, title}`).

### The assistant (`SM.assistant`)

Not an SM overlay, so it never ends up on top of what it opens:

| Screen | Open | What it opens |
|---|---|---|
| ≥ 1024 px | docked at the right (`html.asst-docked`, `--asst-w: 400px`): `.shell` gets `margin-right`, so the page narrows instead of being covered; no scrim, no focus trap | modals centre over the remaining area, panels open next to it (`right: var(--asst-w)`); the scrim dims the assistant |
| < 1024 px | full-height sheet (`html.asst-sheet`, z 70, Tab trapped) | the sheet minimizes to a floating bubble (`#assistant-bubble`, bottom-right, above the phone tab bar) with a status ("Opened New search"); tapping it restores the chat |

`SM.assistant.open({focus})`, `.close()`, `.minimize(status)`, `.toggle()`,
`.handoff(label)` (minimize on small screens after opening something),
`.ask(text, row)` (open with a prefilled question; the row becomes the
highlighted result of the page context). Escape minimizes; `/` (not while
typing) or Ctrl/Cmd+K opens. The open/minimized state survives page loads
(`localStorage["sm-asst-state"]`; a phone comes back as the bubble), and the
chat (messages + rendered cards) is kept per subject in
`localStorage["sm-asst-<subject id>"]` with a Clear button.

Declarative, no JS needed:

| Attribute | Effect |
|---|---|
| `data-modal="/ui/x"` (or `data-modal` + `href`) | open a modal; `data-modal-wide` for wide |
| `data-panel="/ui/x"` + `data-panel-target="id"` | open a side panel |
| `data-popover="/ui/x"` or `"#template"` + `data-popover-class` | popover |
| `data-sm-close` | close the enclosing overlay |
| `data-sm-submit` on a `<form>` | submit via fetch (buttons disabled, no double submit); a fragment answer replaces the overlay, a redirect navigates |
| `data-sm-nav` on a link inside an overlay | load that fragment into the SAME overlay (Runs → one run → back) |
| `data-sm-confirm="Question?"` on a submit button | confirmation dialog first (`data-confirm-title`, `data-confirm-label`); never the browser's `confirm()` |
| `data-autosubmit` on a `<form>` | submit when a control changes (filter selects, compare pickers) |
| `data-sm-refetch="/ui/x?y="` on a `<select>` | reload the overlay with the chosen value appended (`__new__` follows `data-sm-new`) |
| `data-busy-label` on a submit button | label while the request runs ("Creating…") |
| `data-theme-toggle` | switch theme |
| `data-sm-action="/url"` + `data-sm-target="#id"` on a button in a form | POST the form's fields to `/url` (fetch) and put the HTML answer into `#id` (in-dialog Test, "Find my chat id"); without JS it is a normal submit with `formaction` |
| `data-sm-fill="#input"` + `data-value` | put the value into that input (pick a Telegram chat, suggest an ntfy topic) |
| `data-copy="#id"` | copy that element's text (the new API key) |
| `data-show-when="field=a\|b"` | shown only while the form field has one of the values (weekly → day picker, ntfy topic vs URL) |
| `data-fill` / `data-placeholder` / `data-text` (JSON `{selector: value}`) on a radio | choosing it fills inputs, placeholders and helper texts (AI and email presets) |
| `data-secret` + `data-state="saved\|replace\|empty"` | saved secret: "•••• Saved (updated …)" with `data-secret-replace` (reveals the input) and `data-secret-keep` |

Markers a fragment can carry (hidden elements):

| Marker | Effect |
|---|---|
| `data-sm-wide` | the modal is wide (compare, reports) whoever opened it |
| `data-sm-reload-on-close` | reload the page when the overlay closes (something it shows changed: labels, notes) |
| `data-sm-review-count="N"` | update every `[data-review-count]` badge |
| `data-sm-alerts-count="N"` | update the bell badge |

Response headers for fetch submits: `X-SM-Redirect` (navigate),
`X-SM-Toast` (URL-encoded toast text), `X-SM-Panel` / `X-SM-Modal` (close
this overlay and open that one: New search → progress panel).

Deep links: `?panel=/ui/x` or `?modal=/ui/x` (URL-encoded, same-site `/ui/`
or `/assistant/` paths only) open that overlay on load; build them with
`ui_helpers.overlay_url(path, kind)`. Old pages redirect to these.

Use `<a href="/ui/x" data-modal>` so the link still works without JS or in a
new tab.

### Adding a modal or panel (server side)

```python
from dashboard import ui
from dashboard.ui_helpers import fragment_or_page

@router.get("/ui/thing")
async def ui_thing():
    inner = ui.overlay("Title", body_html, foot_html)   # header + close + body + footer
    return fragment_or_page("Title", inner, active="/results")
```

`fragment_or_page` returns the bare fragment to the SM loader and a full page
otherwise. Forms inside should post to normal endpoints that answer with
`context.redirect_with_flash(url, message)`; add `data-sm-submit` to keep
validation errors inside the modal (answer them with the overlay HTML and status
400; a fetch-submitted form gets `X-SM-Redirect` instead of a 303 so the
`#anchor` survives). A fragment answer can carry an `X-SM-Toast` header
(URL-encoded text).

## Results and its overlays

| Route | Kind | Router | Notes |
|---|---|---|---|
| `/results` | page | `results` | filters: `country`, `date`, `classification`, `sort_by`, `order`, `include_homonyms`, `min_severity`, `has_note`, `new_only`, `engine`, `device`; row menus are `<template id='rm-N'>` popovers |
| `/ui/search` | modal | `search_runner` | `?subject=`; posts to `/search_multi` with `subject_id`; answer `X-SM-Panel: /ui/job/<id>` |
| `/ui/job/{id}` | panel | `search_runner` | live progress (app.js `initJob`, polls `/job/{id}/status`): one status box (activity sentence, elapsed, progress bar, "Countries: … · Engines: …"), per-step list in a collapsed Details (open when a step failed); footer "Run in background" / "View results" = `view_url` (switches subject) / "Back to New search" on error |
| `/ui/review` | modal | `review` | one result at a time, `done`/`total`/`skip` state; `?id=` = "Change label" for one result |
| `/ui/compare` | modal | `diff` | `a` (older), `b` (newer), `country`; swaps reversed runs |
| `/ui/runs`, `/ui/run/{id}` | panel | `runs` | "Running now" on top (`search_runner.running_now_html`: the active subject's running jobs, each `data-panel='/ui/job/{id}'`); delete: admin only, `POST /run/{id}/delete` |
| `/ui/timeline` | panel | `timeline` | `?domain=` filters detected events ("Show on timeline") |
| `/ui/evidence` | panel | `evidence` | `?url_hash=` |
| `/ui/ai-checks` | modal | `domains` | More → "AI checks…": "Relabel with AI" (`POST /domains/reclassify-ai`) and "Run AI checks" (`POST /ai/annotate`) with the counts waiting now and the caps; disabled, with a link to Settings → AI provider, without a provider; operator+ |
| `/ui/reports` | modal | `export` | `tab=new|prev`, `kind`, `subject_id`, `country`, `period`; `POST /reports/create`; files via `GET /reports/{subject_id}/{file}` (`export/reports.py` store: `data/exports/<subject_id>/` + JSON sidecar) |
| `/ui/note`, `/ui/notes` | panel | `notes` | `?url=`; `POST /notes/save` (text, status, due date, assignee, comment) |
| `/assistant/panel` | fragment for `SM.assistant` (`#assistant-panel`) | `assistant.router` | client in app.js `initAssistant` (contract in docs/assistant.md) |

Redirects (303): `/runs`, `/run/{id}`, `/timeline`, `/evidence`, `/notes` →
`/results?panel=…`; `/review`, `/diff`, `/export`, `/search_page`
→ `/results?modal=…`; `/domains` and `/ui/domains` (and a `?modal=/ui/domains`
deep link) → `/settings#domains`. `/job/{id}` stays a full page (no-JS
fallback of the progress panel).

Row menu (`results._row_menu`): note, evidence snapshot (negatives), Change
label…, "Mark this domain as mine / negative" (one-click POST
`/domains/add-any`: the same lists as Settings, not a second manager), Show on
timeline, "Ask the assistant about this" (`data-assistant-ask`), open the
page. When the AI set the label, the menu shows its one-line reason
(`ai_reason`). Rows carry `data-result-id/-url/-domain/-label` for the
assistant's page context.

## One home per feature

Every feature has exactly one home; other places may only carry a single
shortcut that opens the same dialog (never a second copy of a form or list):

| Feature | Home | Shortcuts elsewhere |
|---|---|---|
| New search | Results toolbar (`/ui/search`) | Overview: "Run first search" only for a subject with no runs |
| Review | Results toolbar (`/ui/review`) | Overview nudge (a plain link); row menu "Change label…" (same dialog, one result) |
| Compare | Results toolbar (`/ui/compare`) | Overview "What changed": one "Open compare"; Runs: compare a run with the previous one |
| Reports | Results → More (`/ui/reports`) | none (no Overview button) |
| AI relabel + AI checks | Results → More → AI checks (`/ui/ai-checks`) | none |
| Domains | Settings → Projects and subjects → the active subject's Domains (`<details id="domains">`) | row menu "Mark this domain as mine / negative" (action); another subject's "Domains" switches to it |
| Integrations | Settings dialogs (`/ui/integration/<name>`) | the setup wizard renders the same rows/dialogs (Apify step included) |

The assistant's navigate intents point at these homes (`assistant.tools.URL_*`).

Search jobs run under the subject captured at submit time: the worker thread
uses `db.for_subject(sid)` inside `subject_scope(sid)` (threads don't inherit
the request's ContextVar). Scheduled jobs do the same per subject
(`report.weekly.run_scheduled` / `run_alert_checks_scheduled` over
`db.scheduled_subjects()`); the weekly workbook lands in the same report
store, so it shows under Reports → Previous reports ("sent by email" once
emailed). Flat files from before the per-subject store are moved to subject 1
(`export.reports.adopt_legacy_files`, at startup).

The assistant's navigation intents carry these deep links as their fallback
`url` (`assistant.tools.URL_*`, `compare_url`, `deep_link`); the panel client
opens the overlay itself.

Forms in panels answer a fetch submit with the refreshed panel plus
`X-SM-Toast` (so the panel stays usable), and a plain submit with
Post/Redirect/Get to the deep link of the same panel.

## Background searches

A search is an in-memory job (`dashboard/jobs.py`, `JobManager`): one step
per country × engine (`pending → launching → running → saving → done |
skipped | error`); a saving step also carries `stage` (`classify`,
`same_person`, `save`, `severity`, `evidence`), reported by
`report.multi_country.persist_engine_run(on_stage=…)`. A job's `meta`
records the subject (`subject_id`, `subject_name`) and who started it
(`started_by`, `session` = short hash of the session cookie).

`dashboard/job_status.py` turns a job into ONE plain sentence
(`activity_sentence`): "Starting search…", "Searching Google in the United
Kingdom (2 of 6)…", "Waiting for Apify (Google, United Kingdom)… 0:35",
"Saving results…", "Checking same-name results with AI…", "Done: 7 results in
3 countries, 1 new negative", "Search failed: <plain error>". Every view uses
it: the job panel and page, the chip, its popover, Runs → Running now, the
assistant's job card. It travels as `activity` in the JSON.

| Endpoint | What |
|---|---|
| `GET /job/{id}/status` | one job (polled by the open panel every 2 s, 5 s after a minute): steps, `activity`, `summary`, `view_url`… |
| `GET /jobs/active` | no-store JSON: running jobs, and jobs finished in the last 10 minutes (`ACTIVE_FINISHED_SECONDS`) that this viewer hasn't seen, of the subjects the viewer may see; the active subject's in `jobs`, the others in `other_jobs` (+ `other_count`) |
| `POST /jobs/{id}/seen` | hides a finished job for this viewer (`user:<name>`, or `local` without sign-in); same-site only (CSRF rule), allowed for read-only accounts. Opening a finished job's panel/page marks it seen too |

Visibility: a viewer sees (and can open) only jobs of subjects they may see
(`visible_subject_ids`); jobs without a subject stay reachable by id.

Client (`SM.jobs` in app.js): one `GET /jobs/active` on every page load (so
a search survives reloads and navigation), then every 5 s ONLY while
something runs (30 s in a hidden tab, backoff on errors). The chip shows
"Searching… 0:42" (spinner, static under reduced motion), "N searches
running", "Search finished" (dot) or "Search failed" (danger); its text ticks,
so state changes are announced by the separate polite live region
`#jobchip-live`. One job: click opens `/ui/job/{id}`; several: a popover
lists them. A job that finished unseen gets one toast (remembered in
`localStorage["sm-jobs-toasted"]`): "Search for <subject> finished: N
results, M new negatives" + View results (switches subject), or an error
toast + Details. Elements with `data-active-job="<id>"` (Running now, the
popover, the assistant's job card) are updated from the same data. The
assistant's confirmed `start_search` returns a quiet `job` intent (no
auto-opened panel) and a `job` card (`assistant.cards.job_card`) with Open.

## Post/Redirect/Get

Every POST redirects back with `redirect_with_flash(url, message, kind)`
(`kind`: ok | error | info). The message travels in the short-lived `sm_flash`
cookie, is rendered inline by the next HTML page (`ui_helpers.flash_html`, works
without JS), turned into a toast by app.js, and the cookie is deleted on that
response. Say "No changes" when nothing changed; say "Not saved: reason" for
rejected input. A secret that must be shown once (a new API key) goes through
`once=` (process-local store), never the cookie.

## Errors

Browser requests get HTML error pages in the app layout (404, 403, 422, 500;
`main.error_response`). `/api/*`, `/health`, `/jobs/*` and `/job/*/status`
keep JSON.

## Settings

`/settings` is one page: a section nav (left on desktop; a sticky strip of
chips on phones, with "Find a setting") and sections in this order: projects
(with the active subject's Domains inside, `#domains`), search, ai,
notifications (`#routing` inside), scheduling, users, api, audit, backup. A
"setup at a glance" strip on top links to them. app.js opens the `<details>`
an `#anchor` names (so `/settings#domains` lands on the open Domains area).
Non-admins only see Projects and subjects (with Domains); settings writes stay
admin-only in `role_middleware`. Old URLs (`/settings/notifications`, `/settings/api`,
`/settings/backup`, `/users`, `/audit`) redirect to their anchor; `/alerts`
redirects to `/?alerts=1`, which opens the alerts popover.

Every section starts with `integrations.section_head` (title, one plain
sentence, status pill, ONE primary action); details are collapsed below
(`integrations.details`).

### Integrations (`dashboard/integrations.py`, `routers/integrations.py`)

Search (Apify), AI provider, Email, Telegram, Slack and ntfy are rows/cards
with a status pill (`integration_state`: Working / Failing + plain reason /
Not tested yet / Incomplete / Not set up / Optional). "Set up" / "Change" opens
the guided dialog `GET /ui/integration/<name>?next=<url>` (AI: `provider=`,
`preset=`); it shows only the fields of what was chosen. Its Test button posts
the typed values to `POST /settings/integration/<name>/test` before anything is
saved (empty secret = the saved one); Save posts to the usual endpoints
(`/settings/apikey`, `/settings/ai`, `/settings/email`, `/settings/notify`),
which redirect to `next` or, from a fetch submit, answer a rejected value with
the dialog again (status 400, typed secrets never echoed). Telegram's "Find my
chat id" is `POST /settings/integration/telegram/chats` (Bot API getUpdates).
A saved secret's Remove posts `/settings/secret/remove` (`REMOVABLE` says what
goes with it). Test results are kept per configuration fingerprint
(`common/integration_health.py`), so a test of the exact values then saved
becomes the status.

The setup wizard's Apify step and Integrations step render the same rows and
open the same dialogs (`next=/setup`, `next=/setup?step=integrations`); the
wizard keeps only its Skip buttons. `POST /setup/apify` still accepts a key
from older scripts (`first_run_middleware` lets those routes through during
the first run).

AI provider dialog extras: the OpenAI-compatible step has a "Reasoning
effort" select (low / medium / high / off → `AI_REASONING_EFFORT`, saved
through the same validated `/settings/ai` path; the default `low` leaves the
variable unset), and a Test that fails with the empty-answer error adds
"Reasoning models are supported; if this persists, try again or pick a
non-reasoning model." (`llm.reasoning_hint`, also on the Failing status).

Other dialogs: `/ui/settings/schedule/<job>` (frequency, day, time UTC →
`/settings/cron`), `/ui/settings/api-key/new` (the created key is shown once, in
the dialog), `/ui/settings/user/new`, `/ui/settings/user/<id>/password`.
