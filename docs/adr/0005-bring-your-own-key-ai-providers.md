# 0005. Bring-your-own-key AI providers, optional, never the search-time classifier

- Status: Accepted
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

Some tasks the rules cannot do: tell whether a result is about a namesake
("not this person"), score how bad a negative is (1-5), relabel what the
rules left neutral, write the PDF narrative, power the assistant. An LLM
can, but every call costs money and sends result titles, URLs and snippets
to a third party (`DATA.md`; `SECURITY.md`, item 1); the repo ships no keys.

## Decision

AI features are optional and use the operator's own key: "with no
provider configured, it isn't used" (`src/processing/ai_classifier.py`).
Two providers: Google Gemini (default `gemini-2.5-flash-lite`, chosen for
its "generous free quota"), and any OpenAI-compatible endpoint via
`OPENAI_BASE_URL`: OpenAI, OpenRouter, or a local Ollama / LM Studio /
vLLM server (`src/processing/llm.py`; `README.md`, What it does). Every
call goes through one seam, `_call_llm`; the provider is `LLM_PROVIDER`
or inferred from which keys are set. For a non-official base URL
`OPENAI_MODEL` is required: there is "no sensible universal default", so
the provider reports "not configured" instead of guessing (`llm.py`).

The AI is never the classifier: "search-time classification never calls
the AI provider" (`README.md`, Honest limitations) and `save_results`
"must not call AI" (`src/processing/enrich.py`, `src/database/schema.py`).
Same-name detection runs in batches before the save and severity after
it, both best-effort, only for URLs without a prior verdict and, for
severity, only on negatives "to save tokens"; relabelling is a manual
button. A human verdict is never overwritten by the AI (ADR 0002).

## Consequences

### Positive

- Works fully with no AI key; the AI adds information and is not in the
  path of a search or an alert.
- The operator decides where result data goes: "a local endpoint (Ollama,
  LM Studio on your machine) keeps this data on the machine" (`README.md`,
  Where data goes).

### Negative

- "The AI features are not evaluated" with any provider (`README.md`,
  Honest limitations). Without a provider every negative counts as
  severity 3 and same-name handling is the manual button only.
- Two clients to maintain (Gemini native, OpenAI-compatible), and
  OpenAI-compatible servers differ in what they accept.
- No spend cap or fallback provider yet (issues #8, #17); a prompt
  injection guard for SERP text reaching the model is open (issue #9).

## Alternatives considered

- One hard-wired vendor: simpler, but it forces the data flow on everyone.
- AI as the search-time classifier: rejected; the code keeps AI out of
  `save_results` and budgets every call (`enrich.py`), and the AI paths
  have no eval (`README.md`, Honest limitations).
