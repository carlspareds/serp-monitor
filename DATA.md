# Data

The demo data and test fixtures in this repository are fictional. The one
exception is `evals/real_labeled_data.py`, which holds 53 real, public
search results (URL and title) about six well-known public figures and
companies, used to measure the classifier on real data (see
`evals/README.md`).

## Where the code came from

SERP Monitor was extracted from a larger private tool, and all client data
was removed. A fictional subject, "Jordan Ashworth", replaces any real one
everywhere: code, comments, tests, and demo data. The demo seed and test
fixtures use domains under the `.example` TLD, which
[RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserves so they can
never resolve on the real internet.

## What's in `data/`

Nothing, by default. `data/` is gitignored (`data/*.db`, `data/evidence/`,
`data/exports/`) because:

- A populated database contains whatever the operator searched for: real
  names, real people's search results, possibly sensitive allegations.
  That must never end up in git history.
- `data/evidence/` holds downloaded HTML of negative results (see
  `src/processing/evidence.py`), for the same reason.
- `data/exports/<subject_id>/` holds each subject's report files (PDF,
  Excel, CSV, the weekly workbook) with a JSON sidecar per file
  (`src/export/reports.py`): they contain the same data as the database.
- `.env` (real API keys) is gitignored too. `.env.example` ships instead,
  with every secret blank. The only values filled in are non-secret
  defaults such as `SMTP_PORT` and `GEMINI_MODEL`.

## Reproducing demo data

```
.venv/bin/python scripts/seed_demo_data.py
```

Creates `data/seo_dashboard.db` (or `$DB_PATH`) with four weekly US runs for the fictional Jordan Ashworth, results waiting in the review queue, 5 alerts, 3 evidence snapshots, 2 timeline events, one win on Analysis (a negative result that dropped out of the top) and 2 notes on the Notes board. In
the story, a fraud article slowly loses rank while owned pages are
reinforced, and a complaints page appears in week 3. It does
nothing if the database already has a run. `make run` calls it
automatically on first launch.

Titles, domains, and positions are hardcoded in the script. Everything
else comes from the app's own code paths:

- **Labels and confidence** come from the real rule classifier
  (`classify_batch`). Results no rule decides come out "neutral" at 0.5
  and so land in the review queue.
- **Alerts** come from `generate_alerts`, run after each seeded run.
  Notification sending is switched off while seeding.
- **Evidence snapshots** come from `capture_new_negatives`, with the
  network fetch replaced by a small synthetic HTML page that says it is
  synthetic, because `.example` URLs don't resolve. The file write and
  SHA-256 are the real code.
- **Wins** come from `update_url_lifecycle`, run after each seeded run
  just as after a real search.
- **Notes** are created with the same database calls the Notes board
  uses, so their status history is recorded too.

The point is a plausible, inspectable demo, not statistical realism.

## How real data flows

With a real `APIFY_API_KEY` in `.env`, a search launches an Apify actor
(`apify~google-search-scraper` or `tri_angle~bing-search-scraper`). Searches
run on demand from Results → New search (`/search_page`), plus one
scheduled search: the weekly report (Monday 08:00 UTC by default, editable
in Settings → General → Scheduling; Google desktop, the tracked name). The
daily 09:00 UTC job only re-checks alert rules against data already
stored; a daily info-digest email and a weekly purge also run. `src/apify/`
normalizes the actor output, `src/processing/classifier.py` labels each
result with rules (domain lists and keywords), and the `Database` class
saves it to the SQLite file at `DB_PATH`.

Data leaves the machine in these cases:

- **Apify**: every search (query, country, engine).
- **The AI provider**, only if one is set (Gemini or any OpenAI-compatible
  API, see `LLM_PROVIDER` in `.env.example`): result titles, URLs, and
  snippets, for same-name detection, severity scoring, the manual
  "Reclassify with AI" button, and the PDF
  narrative summary. A local endpoint (Ollama, LM Studio on your machine)
  keeps this data on the machine; a hosted one sends it to that vendor.
- **The sites behind newly negative results**: the server fetches each
  one directly, once, to store its HTML as evidence.
- **Notification channels you configure** (email, Telegram, Slack, ntfy).
  If you set only `NTFY_TOPIC`, alerts go to the **public** `ntfy.sh`
  server. Set `NTFY_URL` to use your own.

## Page identity and human verdicts

One page is one URL once tracking parameters (`srsltid`, `utm_*`, `gclid`,
`fbclid`, `mc_cid`, `mc_eid`, `_hsenc`, `_hsmi`, `ref_src`, `igshid`,
`msclkid`) and the `#fragment` are ignored (`normalize_url` in
`src/database/base.py`). New results are stored without them, and
`search_results.url_key` holds the page key (recomputed at startup when the
normalization changes).

A person's verdict wins for the whole page within a subject: labelling a
row (Review, Results, the assistant) applies the same label to every other
row of that page, in every run; new runs inherit it; Relabel with AI and Run
AI checks re-apply it before writing anything and never send that page to
the AI; the review queue never shows a page a person already decided. When
two people disagree, the latest verdict wins; "Not this person" sticks.

Two one-off repair scripts fix data written before this (back up first;
both are idempotent and print what they change):

    # 1. merge url_hash keys of pages that differ only by tracking params
    .venv/bin/python scripts/maintenance/repair_url_identity.py [--db PATH]          # dry run
    .venv/bin/python scripts/maintenance/repair_url_identity.py --apply [--db PATH]

    # 2. re-apply human verdicts to every row of the same page, per subject
    .venv/bin/python scripts/maintenance/repair_human_verdicts.py --dry-run [--db PATH] [--subject ID]
    .venv/bin/python scripts/maintenance/repair_human_verdicts.py [--db PATH] [--subject ID]

`repair_url_identity.py` merges `url_lifecycle` rows of one page (earliest
first seen, latest last seen, best position) and recomputes `evidence.url_hash`;
notes on several spellings of one page are only listed, and old AI-cache
entries are left (they are simply never hit again). Each real run writes a
backup next to the database first (`<db>.pre-url-identity-*.bak`,
`<db>.pre-verdicts-*.bak`).

## Backups and data at rest

Everything above lives in one SQLite file plus `data/evidence/` and
`data/exports/`. A backup from Settings → Backup holds every subject's
results, notes and evidence hashes: store it encrypted and away from
`APP_ENCRYPTION_KEY`. See [`SECURITY.md`](SECURITY.md), item 5.

## Tests

The fixtures in `tests/conftest.py` use the same fictional subject and
reserved `.example` domains as the demo seed, so nothing the test suite
touches refers to anything real.
