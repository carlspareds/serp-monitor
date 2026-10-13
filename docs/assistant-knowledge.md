# SERP Monitor: assistant knowledge base

This file is loaded by the in-app assistant (`src/assistant/prompts.py`). The
`app_help` tool returns one `##` section per topic, so each section must stand
on its own. Keep it factual: where a feature lives in the UI, what it does,
how to use it, and its limits. Never put secrets or example keys here.
Every feature has ONE home; name that home (other places only have a
shortcut that opens the same dialog).

## Overview
SERP Monitor tracks what Google and Bing show for a person, company or brand
(the "subject"). Searches run through Apify. Each result is labelled positive,
negative or neutral by rules (your domain lists plus negative keywords);
results no rule can decide go to the review queue for a human label. Newly
negative pages are saved as evidence with a SHA-256 hash. Alerts fire on new
negatives and thresholds, and can be routed to Email, Telegram, Slack or ntfy.
The left rail (a bottom tab bar on phones) has three pages: Overview,
Results and Settings. Everything else opens as a dialog or side panel: New
search, Review, Compare, Runs, Timeline, Evidence, Notes, Reports and AI
checks live on Results; Domains live with each subject in Settings →
Projects and subjects. The top bar has the Project ▸ Subject switcher, "Find
in stored results", the Assistant, the alerts bell and the account menu
(theme, Help = the Getting started guide at /onboarding, sign out).

## Subjects and projects
A subject is one tracked person, company or brand; a project groups
subjects (for example one client). Each subject owns its own runs, results,
review queue, alerts, evidence, notes, timeline events and positive/negative
domain lists. Name, search query, other names, countries and a short
description for the AI are edited in Settings → Projects and subjects →
Edit. Switch subject from the top bar. The assistant always works on the
active subject only.

## Home
The Overview (/) shows the active subject at a glance for one country: share
of positive and negative results with the change since the previous run, open
alerts, Wins in the last 90 days, the trend per run, share of voice and "What
changed since <previous run>" with one "Open compare" button. When results
wait for review, a short note links to the Review dialog. A subject with no
runs shows one "Run first search" button. New search, Review, Compare and
Reports themselves live on Results.

## Search and runs
Start a search from Results → "New search" (or ask the assistant to prepare
one). Pick countries, engines (Google, Bing when configured), pages (1-3) and
device (desktop or mobile). Every country × engine pair is one Apify run and
costs money on your Apify plan (roughly a few cents each; it takes about 20 to
90 seconds per country and engine). At most 2 searches can run at once. A
monthly Apify budget alert can be set in Settings → Search. Progress shows in
a side panel; you can close it and come back. Results → Runs lists every run
with its date, country, engine, status and counts; open one to see its
results, compare it with the previous run, or delete it (admins). Real
searches need an Apify API key (Settings → Search, or the Setup wizard).
Without one the app is in demo mode with synthetic data.

## Results
Results (/results) lists the stored results of the latest run (or a chosen
run date) for one country: position, title, domain, label, severity and
notes. Filter by country, run date and label; "More filters" adds same-name
results, severity 3+, has a note, only new, engine and device. Each row's ⋯
menu has: add or edit note, save evidence snapshot (negatives), Change
label…, Mark this domain as mine / negative, Show on timeline, Ask the
assistant about this, and open the page. The toolbar has New search, Review,
Compare, Runs and More (Timeline, Evidence, Notes, Reports, AI checks). The
top-bar box "Find in stored results" searches titles and snippets.

## Labels and confidence
How a result gets its label, in priority order: (1) its domain is on the
subject's positive list → positive; (2) it is a public-record source that must
stay neutral → neutral; (3) its domain is on the negative list (blacklist) →
negative; (4) the title, snippet or URL contains a negative keyword (fraud,
scam, lawsuit, criminal, arrested, convicted, indicted, ponzi, embezzle,
allegation, alleged, court, sued...) → negative; (5) otherwise neutral with
confidence 0.5. Rule hits have confidence 1.0. Results below the review
threshold (Settings → Search → Results review) go to the review queue. An AI
provider can relabel what the rules leave neutral and adds same-name
detection and severity scores (1-5) with a topic (legal, financial, personal,
labor, product, opinion, other). AI labels carry a one-line reason, shown in
the row menu and in "Why it's here". Both AI actions live in Results → More →
AI checks: "Relabel with AI" (a random sample of stored results per click;
only neutral ones go to the AI) and "Run AI checks" (same-name and severity
on the latest runs, up to 30 per check per click). A human label is final:
later relabelling never overwrites it. Known limit: the keyword rules miss
negative stories that use none of the keywords (for example a forum complaint
about a refund), and "court" can flag good news (a dismissed case) as
negative. That is what the review queue is for.

## Review queue
Results → Review (the badge shows the count) shows the results no rule could
decide, one at a time. For each one choose Positive, Negative or Neutral (keys
1-3), or "Not this person" (key 4) for a namesake. The verdict is saved as a
human label (confidence 1.0) and audited. A negative verdict also gets a
severity score when an AI provider is configured. "Change label…" in a row's
menu opens the same dialog for one result. The assistant can show a review
card with the same four buttons (operators and admins).

## Compare
Results → Compare compares two runs of the same country: A is the older run,
B the newer one. It shows results that entered, left, moved (with the number
of places) and were reclassified. With no runs selected it compares the two
latest runs. The Overview's "What changed" has an "Open compare" button for
the same dialog, and each run in Results → Runs can be compared with the
previous one.

## Domains
Each subject's positive domains (your own sites, labelled positive) and
negative list (blacklist, labelled negative) live in Settings → Projects and
subjects → the subject's "Domains" area (/settings#domains opens it for the
active subject). A row's ⋯ menu on Results also has "Mark this domain as mine
/ negative", which adds it to the same lists in one click. Adding a domain
does not relabel stored results by itself: click "Apply to stored results"
in the Domains area afterwards. Human labels are never changed by
relabelling.

## Notes
Results → More → Notes is a board of per-URL notes with a status (Open,
In progress, Escalated, Resolved, Dismissed), an assignee and a due date.
Use it to track removal requests, replies and follow-ups. Status changes are
logged in the note's history. Overdue notes are highlighted and can raise an
alert. A row's ⋯ menu opens the note of that result. The assistant can draft
a removal request and save it as a note after you confirm; nothing is sent to
anyone.

## Timeline
Results → More → Timeline records real-world events (press releases,
statements, launches) with an optional expected effect, and shows how the
negative share changed 7, 14 and 30 days after each event. Detected
anomalies are shown too. "Show on timeline" in a row's menu filters it to
that domain.

## Evidence
Results → More → Evidence keeps a downloaded HTML copy of each newly
negative page with its SHA-256 hash and capture time, so you can prove what a
page said even after it changes. Capture is automatic after each search; a
negative row's ⋯ menu also has "Save evidence snapshot". Snapshots can be
downloaded.

## Alerts
The bell in the top bar opens the alerts: new negatives, threshold breaches,
disappeared URLs, overdue notes and system problems. Each alert can be
acknowledged, snoozed for 7 days, or resolved. Settings → Notifications →
Alert routing sets which channel receives each severity (critical, warning,
info; "Daily digest" sends one email a day), the harm-score threshold, and
has "Check now".

## Notifications
Settings → Notifications (admin only) connects Email (SMTP), Telegram (bot
token and chat id), Slack (incoming webhook URL) and ntfy (server URL or
topic). Each one is set up in a short guided dialog with a Test button that
checks the typed values before saving. A channel receives nothing until a
severity is routed to it in Alert routing, just below.

## Telegram setup
1. Create a bot with @BotFather in Telegram and copy its token. 2. Open
Settings → Notifications → Telegram → Set up and paste the token. 3. Send any
message to your bot, then press "Find my chat id" in the dialog and pick the
chat. 4. Press Test, then Save. 5. In Alert routing (same section), route a
severity (for example warning) to Telegram. Only admins can change settings.

## Reports and exports
Results → More → Reports creates an Excel workbook (latest stored run of
each selected country, nothing is re-fetched) or a one-page executive PDF
(KPIs, per-country table, and an AI-written summary when an AI provider is
configured, which uses the AI provider once). Previous reports (also the
weekly workbook) are listed there for download. Settings → Backup offers a
full database download. The assistant can create PDF, Excel or CSV reports
after you confirm and lists previously generated files.

## AI provider
Settings → AI provider (admin only). Choose Gemini (needs a Gemini API key)
or any OpenAI-compatible API: OpenAI, OpenRouter, OpenCode, or a local server
such as Ollama or LM Studio (base URL including /v1, and a model name). For
OpenAI-compatible APIs, "Reasoning effort" (low, medium, high, off) sets how
much a reasoning model thinks. Press Test to check it. The AI provider powers
same-name detection, severity scores, AI checks, the PDF summary and this
assistant. Reasoning models (DeepSeek, o-series, Gemini 2.5 thinking) work:
the app gives every call at least 1024 output tokens (2048 for the PDF
summary and the assistant) and retries a cut-off answer once with double the
budget. "The model returned no answer" means the model used its whole budget
thinking; try again or pick a non-reasoning model. Without a provider, labels
come from the rules only and the assistant is off.

## Apify
Settings → Search (admin only) holds the Apify token (guided dialog with
Test), this month's spend and the optional monthly budget alert. The Setup
wizard (/setup) opens the very same dialog on first run.

## Schedule
Settings → Scheduling lists the scheduled jobs (weekly search, daily alert
check, digest, purges) with when they run, the last status and the next run.
Admins can change the schedule or pause a job.

## Users and roles
Settings → Users (admin only) creates accounts with one of three roles:
admin (everything, including settings, users, audit and backups), operator
(day-to-day work: searches, review, notes, alerts, domains, reports, AI
checks) and viewer (read-only). The assistant follows the same rules: viewers
can ask questions and open pages; actions need operator or admin.

## Audit log
Settings → Audit log (admin only) is a tamper-evident log of every write
action (who, when, what, from which IP), chained with SHA-256 hashes; it can
be exported as CSV. Every action the assistant performs after your
confirmation, and every cancelled proposal, is recorded there.

## API
Settings → API keys (admin only) creates keys for the JSON API at /api/v1
(read scope, or write:alerts to update alert status), used by scripts or
n8n. The key is shown once.

## Setup wizard
The Setup wizard (/setup) is the guided first-run setup: Apify token,
integrations (AI provider, notifications), first admin account and first
subject. It opens the same dialogs as Settings. Each optional step can be
skipped and resumed later.

## Assistant
The assistant (top bar, or press / or Ctrl+K) answers questions about the
app and the active subject's data, knows which page and result you are
looking at, suggests next steps, and opens pages and dialogs for you. It can
list the latest run's results (position, title, domain, label, whether a
person, a rule or the AI set it, and the same-person flag), for one country
or one label, e.g. "a table of the current results by label". On a
wide screen it docks at the right and the page narrows; on a phone it
minimizes to a bubble when it opens something. It can prepare actions (start
a search, create a report, save a note, set a result's label, acknowledge an
alert, add a domain); you can adjust the countries, engines, report type or
note text on the card, and nothing that costs money or changes data happens
until you click its confirmation button; Cancel changes nothing. It uses the
AI provider configured in Settings → AI provider.
