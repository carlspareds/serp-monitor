# Features

What SERP Monitor does, feature by feature, in detail. The short version is
in the main README; this file is the reference.

[Back to README](../README.md)

## What it does

- **Searches** Google (desktop and mobile) and Bing (desktop only) through
  Apify's hosted scraper actors, across the countries you pick, on demand
  from Results → New search (a dialog; progress follows in a side panel). The only *scheduled* search
  is the weekly report: Monday 08:00 UTC by default (editable in Settings →
  General → Scheduling), Google desktop, the tracked name of every active,
  non-demo subject (see [Projects and subjects](projects-and-subjects.md)). The scheduler
  also sends a daily info-digest email, runs a weekly purge and writes a
  nightly backup of the database to `data/backups/` (14 days kept; see
  [Install and run](../README.md#install-and-run) for the restore script).
- **Classifies** every result with rules only: your positive-domain list,
  a negative-domain blacklist, and a short negative-keyword list
  (`config/positive_domains.py`). A rule hit gets confidence 1.0; a result
  no rule decides is "neutral" at 0.5, which is below the default review
  threshold (0.7), so it lands in the human review queue (Results → Review).
  There, a person confirms or corrects the label, and that verdict is not
  overwritten by later bulk reclassification.
- **Alerts** on stored data: a negative-result threshold per country
  (weighted by severity), a URL that is newly negative compared with the
  previous run, a tracked negative URL dropping out of the results, Apify
  budget reached, a scheduled job missing its window, and an overdue note.
  These rules run after a weekly-report run, in a daily 09:00 UTC check
  over what is already stored, and from a manual button on `/alerts`. An
  on-demand search does not run them itself, except two that can fire on
  any search: a tracked negative URL dropping out (`url_gone`) and the
  Apify budget (`apify_budget`). There is no ranking-drop alert. Alerts go
  to email, Telegram, Slack, or [ntfy](https://ntfy.sh) according to a
  per-severity routing table on the Alerts page (`/alerts#routing`; by
  default, `warning` alerts go to email only).
- **Captures evidence** for newly negative results: a best-effort download
  of the raw HTML of the *result page itself* (not of the search results
  page), once per URL, at most 10 per run, skipping results marked as a
  different person. A negative result can also be captured by hand from
  Results (the result's ⋯ menu), and Results → More → Evidence lists every snapshot. The file's SHA-256 is stored in the same SQLite
  database, which lets you check that the file hasn't changed since; it is
  not a legal chain of custody. Evidence is purged after 365 days by
  default (the `PURGE_EVIDENCE_DAYS` row of the `settings` table; there is
  no UI for retention).
- **Optional AI provider features** (Gemini or any OpenAI-compatible API,
  such as OpenAI, OpenRouter, or a local Ollama / LM Studio server; set in
  Settings → General → AI provider): same-name (not this person)
  detection, a 1-5 severity score for negatives, a manual "Relabel with
  AI" button (AI labels come with a one-line reason, shown in "Why it's
  here"), a "Run AI checks" button that re-runs same-name and severity on
  the latest stored results that don't have them yet (for example after
  the provider was down during a search; capped at 30 results per check
  per click), and a narrative summary in the PDF report (left out, with a
  one-line "AI summary unavailable: reason" note, when the AI call fails).
  Without a provider, the only same-name handling is the manual
  "Not this person" button in the Review dialog, and every negative counts as
  severity 3. The AI paths have no eval.
- **Reasoning models work** (DeepSeek, OpenAI o-series/gpt-5, Gemini 2.5
  "thinking", Qwen3...). They spend part of the output budget on hidden
  reasoning, so every AI call asks for at least 1024 output tokens (labels,
  severity, same-name, the Settings test) or 2048 (PDF summary, assistant).
  An empty answer is reported as a failure ("The model returned no
  answer...") instead of an empty success; a reply cut off by the token
  limit is retried once with double the budget (at most 8192). For
  OpenAI-compatible servers the app sends `reasoning_effort` (default
  `low`, `AI_REASONING_EFFORT` to change or `off`) and falls back to
  `max_completion_tokens` when a server rejects `max_tokens`; a server
  that rejects either is remembered for that model until restart.
- **Reports and integrations:** a weekly summary email, a PDF summary and
  an Excel workbook export, and a JSON API (`/api/v1`, API keys with
  scopes, optionally limited to one subject) that reads results/runs/
  alerts/stats per subject and can update an alert's status
  (`PATCH /api/v1/alerts/{id}`).
- **Records** a timeline of real-world events, a run-to-run comparison (Compare, older run A against newer run B), and an
  audit log of every write made through the dashboard.

## Screens and workflow

Three destinations in the left rail (a bottom tab bar on phones):
**Overview**, **Results** and **Settings**. Everything else opens as a dialog
or a side panel over the page you are on, so you never lose your place.
The top bar has the *Project ▸ Subject* switcher, "Find in stored results",
the Assistant, the background-search chip, the alerts bell and the account
menu.

**Background searches.** While a search runs, a chip next to the bell says
"Searching… 0:42" ("2 searches running" when several); it turns into "Search
finished" or "Search failed" afterwards and stays until you open it. Clicking
it opens the search's progress panel (or a short list when there are
several). It survives reloads and page changes, and a toast tells you when a
search finishes ("Search for Jordan Ashworth finished: 7 results, 1 new
negative", with View results) or fails (with Details).

**Results** shows the active subject's latest run (or a chosen run date) for
one country: a one-row filter bar (country, run date, label chips, "More
filters": same-name results, severity 3 or higher, has a note, only new since
the last run) and a sortable table (position, result, label with a "needs
review" tag, severity, note and evidence tags). Each row's ⋯ menu: add or
edit a note, save an evidence snapshot (negatives), change the label, mark
the domain as yours or negative (one click, same lists as Settings), show the
domain on the timeline, ask the assistant about it, open the page. Results is
the one home of New search, Review, Compare, Reports and the AI checks; the
Overview only has single shortcuts to the same dialogs. The toolbar's tools
are listed in the README (How to use it); how the shell, dialogs and panels
are built is in [`ui-architecture.md`](ui-architecture.md).

Domains (your positive domains and the negative list, plus "Apply to stored
results") live with each subject in **Settings → Projects and subjects**.

Old addresses (`/runs`, `/run/<id>`, `/diff`, `/review`, `/timeline`,
`/evidence`, `/notes`, `/export`, `/search_page`) redirect to Results with
the matching dialog or panel open, and `/domains` to the active subject's
Domains in Settings, so bookmarks keep working.

## In-app pages worth knowing

- `/setup`, a setup wizard: Apify key, optional integrations (AI provider,
  email, Telegram, Slack, ntfy), the admin account (already done if you
  created it on the first visit; it can't be skipped), and the tracked
  name. The other steps are skippable and the wizard resumes where you
  left off, because it reads the real persisted config. Re-opening a step and
  submitting values saves them even after it was skipped. Everything it
  sets can also be changed later in Settings (General and Notifications).
- `/onboarding`, the Getting started guide: a checklist that reflects what
  is actually configured.

A demo-mode banner stays on every page until a real Apify key is set.
