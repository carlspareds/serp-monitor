# In-app assistant (backend)

A chat assistant that knows every feature of SERP Monitor and the active
subject's data, suggests next steps, and acts only through defined tools.
Anything that costs money or changes data needs an explicit confirmation
click, and every executed (or cancelled) action is written to the audit log.

It uses the AI provider configured in Settings → AI provider
(`src/processing/llm.py`: Gemini, or any OpenAI-compatible API such as OpenAI,
OpenRouter, OpenCode Go, Ollama or LM Studio). With no provider configured the
assistant is off and says so ("set up an AI provider in Settings → AI
provider").

## Code map

| File | What it holds |
| --- | --- |
| `src/assistant/tools.py` | Tool registry: name, description, JSON schema, kind, `requires_confirmation`, `min_role`, executor (and `prepare` for actions). Argument validation. |
| `src/assistant/harness.py` | Provider-agnostic tool loop, OpenAI/Gemini adapters, JSON fallback protocol, guards, secret scrubbing. |
| `src/assistant/prompts.py` | System prompt (role, rules, compact knowledge base, user role, subject summary) and the JSON-protocol prompt. |
| `src/assistant/pending.py` | Pending-action store for the confirmation protocol. |
| `src/assistant/suggest.py` | Suggestion chips, the greeting and the follow-up chips after a reply (deterministic, no LLM call). |
| `src/assistant/cards.py` | Card events built from tool results (results, changes, review, report, share of voice). |
| `src/assistant/router.py` | The HTTP API and the panel fragment. |
| `src/assistant/fakes.py` | Scripted models for tests and offline evals. |
| `docs/assistant-knowledge.md` | The app knowledge base (one `##` section per feature). `app_help` returns sections from it; the system prompt carries a compact version. |

Wiring in `src/dashboard/main.py`: one import, one `include_router`, and
`role_middleware` exempts `POST /assistant/chat` and `POST /assistant/stop`
from the read-only block so viewers can chat. CSRF (Origin check) still
applies to every assistant POST.

## Tools

All tools run on the active subject (`database.base` ContextVar, set per
request by `dashboard.context.SubjectMiddleware`) through a `db.for_subject()`
instance, using existing Database/processing functions.

**read** (run immediately)

| Tool | Returns |
| --- | --- |
| `get_overview` | KPIs, latest run per country/engine with delta and entered/left results vs the previous run, open alerts, pending review count, wins (90 days), overdue notes, an `attention` list |
| `list_changes(country?, engine?, run_a?, run_b?)` | Diff of two runs (`processing.diff.run_diff`): entered, left, moved, reclassified, plus `compare_url` |
| `list_review_queue(limit)` | Pending items with a `why` per item |
| `list_results(country?, label?, limit≤50)` | The latest run's results per country (or one country / one label) in position order: position, title, domain, label, `source` (human/rule/ai), `same_person`, plus `count_by_label` (namesakes not counted); a `results` card with up to 10 rows |
| `explain_result(result_id \| url)` | Label, confidence, who labelled it (rule/ai/human), which rule hit (positive domain, public record, blacklist, keyword), review status, namesake flag, severity, evidence, note |
| `list_alerts(status, limit)` | Alerts by status |
| `list_runs(limit)` | Recent runs with counts and cost |
| `list_notes(status?, overdue_only?)` | Notes board items |
| `get_setup_status` | Apify / AI provider / channels / routing / scheduled jobs: booleans and labels only, plus a `missing` list. Never a secret value. |
| `list_reports` | Report kinds and previously generated files (download links) |
| `app_help(topic)` | Knowledge-base sections matching the topic |

**navigate** (no confirmation; the result is a UI intent pointing at the
feature's ONE home, see docs/ui-architecture.md "One home per feature")
`open_review`, `open_compare(run_a?, run_b?)`, `open_runs`, `open_timeline`,
`open_evidence`, `open_domains` (Settings → Projects and subjects → Domains,
`/settings#domains`), `open_reports`, `open_ai_checks` (Results → More → AI
checks), `open_search` (the New search dialog; operator+), `open_notes`,
`open_alerts`, `open_settings(section)` (admin only), `open_result(result_id)`.

**action** (`requires_confirmation`, operator or admin)

| Tool | Confirmation card shows | Executes |
| --- | --- | --- |
| `start_search(countries?, engines?, pages?, device?)` | query, countries, engines; estimated Apify cost (average of past runs, or a rough default), time (20-90 s per country × engine), month spend vs budget | Same JobManager + `run_multi_country` path as `POST /search_multi`, with the subject carried into the worker thread; returns the job id and a `/job/<id>` intent |
| `create_report(kind pdf\|xlsx\|csv, countries?, period?)` | type, countries, period; AI cost note for the PDF summary | Builds the file in `data/exports/`; download at `/assistant/reports/<file>` |
| `save_note(url, text, status?)` | URL, full text, whether it replaces an existing note | `set_note` (+ `set_note_status`); nothing is sent |
| `set_result_label(result_id, label)` | result, old → new label | `set_review_verdict` (human verdict) or `set_same_person_verdict` for `not_this_person` |
| `acknowledge_alert(alert_id)` | the alert | `ack_alert` with the user as assignee |
| `add_domain(domain, list)` | domain (normalised), effect, "reclassify to relabel stored results" | `add_positive_domain` / `add_negative_domain` |

Viewers are only offered read/navigate tools (and not `open_settings` or
`open_search`). The role check is repeated when a tool is dispatched and again
at confirm time.

**Editable confirmations.** `prepare` also returns an `edit` spec that the
card renders as controls; the user can change only these (`tools.EDITABLE`):

| Tool | Editable | Control |
| --- | --- | --- |
| `start_search` | `countries`, `engines` | toggle chips: the subject's countries (preselected: the proposal) and only the configured engines; live cost/time estimate from `edit.estimate` (`per_run`, `measured`, `pages`, `budget`, `spent`) |
| `create_report` | `kind`, `period` | type radio (PDF / Excel / CSV), period select |
| `save_note` | `text` | textarea (≤ 4000) |

`POST /assistant/confirm {id, args}` re-validates the edits before claiming
the action (`tools.apply_edits`): keys outside `EDITABLE` → rejected; chip,
radio and select values must be among the options the card offered (no
unknown engine/country/type); the merged arguments go through the schema and
the tool's `prepare` again. A rejected edit answers 400
`{error: "invalid_args", message}` and the proposal stays pending; roles and
the subject check run first. The executed (edited) args are what the audit
row records, with `edited: true`.

## Harness

`run_turn(model, ctx, messages, session_key=...)`:

1. Cleans the client transcript (only `user`/`assistant` text, last 20
   messages, 4000 chars each; a client can't inject system or tool messages).
2. Builds the system prompt with the subject overview and the **page
   context** the panel sent (`clean_context`: page path, its title, the open
   overlay, the highlighted result `{result_id, title, domain, url, label}`
   and the Results filters; unknown keys dropped, strings clipped, marked as
   data). "Why is this one negative?" uses the highlighted result's id.
3. Loops up to **6 model calls** (max 4 tool calls per call, 100 s budget,
   45 s HTTP timeout, 2048 output tokens). Read tools run and their JSON
   result (capped at 6000 chars) goes back to the model; navigate tools emit an
   `intent` event; action tools call `prepare` (validation + card) and create a
      pending action, and the model is told "NOT executed, the user must confirm".
   At most 3 proposals per turn. Each read tool's result can also become a
   **card** event (`cards.from_tool`; one per kind, at most 4 per turn).
   Progress: `on_step(label)` reports "Thinking…", the tool's step
   ("Comparing the latest runs…", `harness.STEP_LABELS`), "Writing the
   answer…"; `should_stop()` (the user pressed Stop) ends the turn before the
   next model call or tool with `stopped="cancelled"`.
4. After an `explain_result` call, if the final reply doesn't mention the
   label the tool returned (e.g. the model only said "I've opened the
   Results page"), the tool's plain explanation (label, who/what set it, the
   rule or the AI's one-line reason, review/same-name/severity notes) is
   appended to the reply. The system prompt also tells the model to answer
   from read-tool results and never to count acknowledged alerts as open
   (the greeting and chips count `open` and `acknowledged` separately).
5. **Claims vs events.** The reply may not describe a confirmation card, a
   prepared action, an opened screen or a saved/created item that no tool
   call of this turn produced (`harness.claimed_kinds`: en/es/fr patterns,
   negated sentences such as "it's not saved yet" ignored). On such a claim
   the model is re-prompted ONCE with `CORRECTIVE_NOTE` ("You described an
   action but didn't call a tool. Call the appropriate tool now, or say
   plainly that you didn't do it."). If it still has no matching event, the
   claim's sentences are dropped and replaced by "I couldn't prepare that.",
   plus a deterministic fallback when the request obviously maps to one
   tool (`harness.fallback_call`: open search / compare / review / reports /
   alerts / notes ..., or a `create_report` proposal with default args). A
   fallback intent carries `fallback: true` and is shown as a button, never
   opened automatically; a fallback proposal is an ordinary confirmation card.
   Nothing ever executes without Confirm.
6. Errors become plain messages: unknown tool / bad args / ToolError are fed
   back to the model; a crashing tool returns a generic error; provider errors
   (401/403, 404, 429, 5xx, network) end the turn with a readable reason.
7. Reply and events are scrubbed of every env value whose name looks secret
   (KEY, TOKEN, SECRET, PASS, WEBHOOK...), and the router scrubs whole responses
   again.

Reasoning models (DeepSeek, o-series, Gemini 2.5 thinking...) spend part of
the output budget on hidden reasoning. The 2048-token budget leaves room for
it; when a reply comes back empty with `finish_reason=length` (Gemini:
`MAX_TOKENS`) the call is retried once with double the budget (capped at
8192), and an answer that is still empty ends the turn with "The model
returned no answer (it may be a reasoning model that needs more output
tokens)." That failure is also recorded in Settings → AI provider status.
For OpenAI-compatible servers the request goes through
`llm.post_chat_completion`, shared with the rest of the app: it sends
`reasoning_effort` (`AI_REASONING_EFFORT`, default `low`, `off` to disable)
and drops it when the server answers 400/422 for it, and it switches
`max_tokens` to `max_completion_tokens` when the server asks for that. Both
fallbacks are remembered per server+model until restart. A 400 about tools
still triggers the JSON fallback below.

The other AI features use the same rules (`src/processing/llm.py`): at least
1024 output tokens for labels, severity, same-name and the Settings test,
2048 for the PDF summary; an empty answer is a failure, never "ok" with
nothing, and no "(truncated...)" marker ever reaches the UI, the PDF or the
assistant. To re-run same-name and severity on stored results after the
provider failed during a search, use **Run AI checks** (`POST /ai/annotate`,
operator or admin) next to "Relabel with AI" in Results → More → AI checks
(`/ui/ai-checks`, which shows the counts waiting and the caps first): it
covers the latest run of each country/engine/device of the active subject, up
to 30 results per check per click, and reports checked / set / failed counts
with the first failure reason (audited as `ai_annotate`). When the last
failure is the empty-answer one, Settings → AI provider, the Test result and
the assistant add `llm.REASONING_HINT` ("Reasoning models are supported; if
this persists, try again or pick a non-reasoning model.").

Providers:

- OpenAI-compatible: `POST {OPENAI_BASE_URL}/chat/completions` with `tools` /
  `tool_choice: auto`, parsing `tool_calls`; bearer key in a header.
- Gemini: `generateContent` with `functionDeclarations` (schemas converted to
  Gemini's subset; no-argument tools have no `parameters`), `functionCall` /
  `functionResponse`; the key goes in the `x-goog-api-key` header, never the URL.
- JSON fallback: when the provider rejects tools (HTTP 4xx/5xx mentioning
  tools/functions, or Gemini `MALFORMED_FUNCTION_CALL`), the harness switches
  to a protocol where the model answers `{"tool": ..., "args": ...}` or
  `{"reply": ...}`, parsed defensively (code fences, text around the JSON,
  string-encoded args; anything unparseable is treated as the reply). The
  switch is remembered per provider+model. `ASSISTANT_TOOL_MODE=auto|native|json`
  overrides it. Models that put a tool call as JSON text in `content` are also
  accepted.

## Confirmation and audit

- A proposal is a `PendingAction` with a random id (`secrets.token_urlsafe`),
  bound to the session (hash of the `sid` cookie; with auth disabled, a
  per-browser `asst_sid` cookie), the user and the subject, expiring after
  10 minutes. Stored in memory: a restart drops proposals, which is the safe
  direction.
- `POST /assistant/confirm {id}`: unknown or expired → 404; another session → 403;
  role below the tool's `min_role` → 403; subject switched since → 409.
  Otherwise the action is claimed atomically (pending → executing), executed
  once, and audited as `assistant_action` with actor, `subject_id`, tool,
  sanitised args (schema keys only, long text clipped), `pending_id`, `ok` and
  the result message. The audit resource is the real one (domain, url, alert,
  job, report) so `/audit?resource_id=...` finds it. A repeated confirm returns
  the stored result with `already: true` and doesn't execute or audit again.
  A failed execution is audited with `ok: false`.
- `POST /assistant/cancel {id}`: same 404/403 rules; marks it cancelled and
  audits `assistant_action_cancelled`. Nothing changes; a later confirm → 409.

## API

| Endpoint | Body / response |
| --- | --- |
| `GET /assistant/status` | `{enabled, reason, message, settings_url, health, provider: {provider, model, host}, tool_mode, role, can_act, subject: {id, name}}`; `health` = `{ok: false, reason, at, hint}` when the last AI call failed, else null |
| `POST /assistant/chat` | `{messages: [{role, content}], context?, turn?}` → `{messages (cleaned transcript + new assistant reply), reply, events, suggestions, followups, progress, error, hint}` (`stopped: true` when Stop won). `turn` (6-40 chars `[A-Za-z0-9_-]`) enables progress/Stop. 503 `{error: "assistant_disabled", message}` without a provider; 400 bad body; 429 over 20 turns/min per session. |
| `GET /assistant/progress?turn=` | `{step, steps, done}` of a running turn (same session only) |
| `POST /assistant/stop` | `{turn}` → `{ok}`; the turn stops before its next step and the client ignores a late answer |
| `POST /assistant/confirm` | `{id, args?}` → `{ok, already, id, message, result, events, edited}`; 400 `invalid_args` for a disallowed edit (still pending) |
| `POST /assistant/cancel` | `{id}` → `{ok, id, message}` |
| `GET /assistant/suggestions` | `{suggestions: [{id, label, prompt}]}` |
| `GET /assistant/panel` | HTML fragment (below) |
| `GET /assistant/reports/<file>` | A report file from `data/exports/` (name whitelist; no paths) |

Events in a chat response, in order:

```json
{"type": "tool", "name": "list_changes", "summary": "Used: compare runs 29 Sep → 6 Oct (US)"}
{"type": "intent", "tool": "open_review", "target": "review", "url": "/results?modal=/ui/review", "label": "Review queue", "params": {}}
{"type": "card", "kind": "changes", "tool": "list_changes", "data": {"country": "US", "from": "29 Sep", "to": "6 Oct",
 "summary": {"entered": 1, "exited": 0, "new_negatives": 1}, "items": [{"kind": "entered", "domain": "forum.example", "position": 5, "label": "negative"}],
 "compare_url": "/results?modal=/ui/compare%3Fa%3D…", "params": {"run_a": "…", "run_b": "…"}}}
{"type": "confirm", "id": "…", "tool": "start_search", "title": "Run this search now?",
 "details": ["Search: Jordan Ashworth", "Countries: US", "Engines: Google", "Pages: 1 · Device: desktop"],
 "cost_note": "Uses your Apify plan: about $0.04 (1 run). Takes about 20-90 seconds.",
 "button": "Run search", "expires_in": 600,
 "edit": {"fields": [{"name": "countries", "type": "chips", "label": "Countries", "options": [{"value": "us", "label": "US"}], "value": ["us"]},
                     {"name": "engines", "type": "chips", "label": "Engines", "options": [{"value": "google", "label": "Google"}], "value": ["google"]}],
          "estimate": {"per_run": 0.04, "measured": true, "pages": 1, "budget": null, "spent": null}}}
```

Card kinds (`assistant/cards.py`, data only from tool results): `results`
(explain_result, or the review queue for viewers: title, domain, label pill,
Open / Review), `changes` (list_changes: counts, top rows, "Open compare"),
`review` (list_review_queue for operator+: up to 3 items with Positive /
Negative / Neutral / Not this person, which POST the same audited endpoints as
the Review dialog with `mode=chat` → JSON `{ok, message, pending}`), `report`
(list_reports' previous files, or the file a confirmed create_report made:
Download), `sov` (get_overview: positive/negative/neutral per country).

Follow-ups (`suggest.follow_ups`): 2-3 next-step chips derived from the
turn's tool results (e.g. after list_changes with a new negative: "Draft a
removal request for forum.example", "Why is forum.example negative?"),
falling back to the state chips; never the question just asked.

Intent targets: `review`, `compare`, `runs`, `timeline`, `evidence`,
`reports`, `ai_checks`, `search`, `notes` (overlays over Results), `domains`
(the page `/settings#domains`), `alerts` (the bell), `settings`
(params.section), `result` (params.result_id, run_id, result_url), and from
confirm results `job` (params.job_id), `download` (params.file; shown as a
report card). `url` is always a working page URL to fall back on.

Suggestion chips (max 5) come from state: pending reviews, new negatives or
changes in the latest run, open alerts, overdue notes, failed searches or
scheduled jobs, "run a search" (operators/admins), "alerts on Telegram" when
no channel is configured (admins), "make a report".

## Panel fragment contract (for the UI)

`GET /assistant/panel` returns one element (and, with auth disabled, sets the
per-browser `asst_sid` cookie so the first turn's progress/Stop match):

```html
<section class="assistant-panel" id="assistant-panel" data-assistant
  data-enabled="true" data-role="operator" data-can-act="true"
  data-subject-id="1" data-subject-name="…"
  data-chat-url="/assistant/chat" data-confirm-url="/assistant/confirm"
  data-cancel-url="/assistant/cancel" data-suggestions-url="/assistant/suggestions"
  data-progress-url="/assistant/progress" data-stop-url="/assistant/stop"
  data-status-url="/assistant/status" aria-labelledby="assistant-title">
  <header class="assistant-head ov-head"><h2 id="assistant-title" data-sm-title>Assistant</h2>
    <div class="assistant-tools">
      <button data-assistant-clear>…</button><button data-assistant-minimize>…</button>
      <button data-assistant-close>…</button></div>
    <p class="assistant-provider">provider · model · / or Ctrl K to open</p></header>
  <div class="assistant-transcript" id="assistant-transcript" data-assistant-transcript role="log" aria-live="polite">
    <div class="assistant-msg assistant-msg-bot" data-msg-role="assistant" data-greeting>…</div>
    <!-- when the last AI call failed: -->
    <div class="assistant-card assistant-card-warn" data-assistant-health>… reason, reasoning hint, Settings link</div>
  </div>
  <div class="assistant-chips" id="assistant-chips" data-assistant-chips>
    <button class="assistant-chip" data-assistant-chip data-chip-id="review" data-prompt="…">…</button>
  </div>
  <form class="assistant-composer" id="assistant-composer" data-assistant-composer>
    <textarea id="assistant-input" data-assistant-input maxlength="4000"></textarea>
    <button class="assistant-send" data-assistant-send>Send</button>
    <button class="assistant-stop" data-assistant-stop hidden>Stop</button>
  </form>
  <template id="assistant-confirm-template">
    <div class="assistant-confirm" data-assistant-confirm-card data-confirm-id="">
      <p data-confirm-title></p><ul data-confirm-details></ul><p data-confirm-note></p>
      <button data-assistant-confirm>Confirm</button><button data-assistant-cancel>Cancel</button>
    </div>
  </template>
</section>
```

Disabled: `data-enabled="false"`, a friendly `data-assistant-disabled` card
with a link to `/settings#ai` (admins; others are told to ask an admin) and no
composer.

Client (`static/app.js`, `SM.assistant` + `initAssistant`; layout in
docs/ui-architecture.md): the fragment is loaded once into the shell's
`<aside id="assistant-panel">`, docked on desktop and a sheet → bubble on
smaller screens. Each turn POSTs `{messages, context, turn}`: `context` is the
page path and title, the top SM overlay, the highlighted Results row
(`tr.is-active[data-result-id]`, or the row whose ⋯ menu said "Ask the
assistant about this", which prefills the question) and the Results filters.
While waiting, a "thinking" line shows the step from `GET /assistant/progress`
(polled every 0.9 s; staged fallback texts if it has none) and Stop replaces
Send (aborts the fetch, POSTs `/assistant/stop`, ignores a late answer).
Events render in order: tool → "Used: …" line; card → a small card per kind;
intent → an "Open …" button in the chat and, once the whole turn (events
and reply) is saved to localStorage, the overlay (or page) of its home opens
ON TOP and the assistant hands off (`SM.assistant.handoff`: minimize to the
bubble on small screens) -- a fallback intent (`fallback: true`) only shows
the button; confirm → the template plus the editable
controls, a live estimate for searches, Confirm sends only the changed
`args` (a 400 `invalid_args` shows its message on the card, still
actionable). Cards always render ABOVE the reply (the prompt says "above"). Then the
reply (markdown-lite: **bold**, `code`, `#`/`##` headings as bold lines,
`| simple | tables |` as escaped HTML tables, bullet and
numbered lists, links only to in-app paths `[text](/path)`, opened in place
when they are `?modal=`/`?panel=` deep links), an error card with the
reasoning hint and a Settings link when the provider failed, and the
`followups` as "Next steps" in the chip bar right under the reply (the state
`suggestions` when there are none). The
transcript (messages + rendered entries) is stored per subject in
localStorage; Clear empties it.

## Tests and evals

- `tests/test_assistant_tools.py`: registry schemas, every read tool on the
  seeded demo DB, navigate intents (single homes), validation, subject
  scoping, `prepare` has no side effects, executors, editable-card
  validation (`apply_edits`), cards from tool results, follow-ups, page
  context cleaning.
- `tests/test_assistant_harness.py`: loop with scripted models, max-steps and
  time guards, errors, viewer gating, JSON protocol parsing and fallback,
  OpenAI/Gemini payloads against mocked HTTP, secrets never in the prompt or
  reply.
- `tests/test_assistant_api.py`: disabled state, chat, confirm once + audit,
  cancel, expiry, other session, subject switch, roles, CSRF, rate limit,
  start_search job, report download, suggestions, panel, page context in the
  prompt, card events, progress + Stop, confirm with edited args (accepted,
  rejected and still pending, viewer refused), the review card's verdict
  endpoint, the reasoning hint.
- `tests/test_ui_consistency.py`: one home per feature (no Domains section,
  nav item or Results menu entry; no Reports/New search on the Overview; the
  AI checks dialog) and static checks of `static/app.js` (SM z-index stack,
  focus fallback, the assistant's handoff/minimize on open).
- `evals/assistant/` (see `evals/README.md`): 28 request → behaviour cases,
  offline plumbing mode and `--live` against the configured provider.
