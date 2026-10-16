# 0002. Rule-based classification with confidence and a human review queue

- Status: Accepted
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

Every stored result gets a label (positive, negative, neutral) that feeds
the alerts and reports. The labels are about a person's or a company's
reputation, so a wrong one is costly, and the app must work with no AI
key at all (ADR 0005). The README's design statement: "don't auto-decide
the ambiguous cases, keep a record of what informed each decision, and
log who changed what" (`README.md`, Design).

## Decision

Search-time classification uses rules only
(`src/processing/classifier.py`): positive domain > force-neutral
(public-record sites) > negative-domain blacklist > negative keyword >
neutral by default. The lists live in `config/positive_domains.py` and
per subject in Settings. Every rule hit returns `confidence=1.0`; the
"neutral by default" case returns `0.5`, below the review threshold
(default 0.7), so it lands in the review queue (Results → Review). A
person confirms or corrects the label there; `classified_by='human'` rows
are "IMMUTABLE against mass reclassifications" (`src/database/schema.py`,
`reclassify_all` in `src/database/results.py`), and the verdict follows
the page across runs through a normalized URL key
(`src/database/base.py::normalize_url`).

## Consequences

### Positive

- Deterministic, free, offline, explainable: every label traces to a
  domain or a keyword.
- Uncertain cases are visible as a queue instead of silently guessed, and
  human work is never undone by automation.

### Negative

- The rules do not work on real data. Against 53 real results the
  classifier predicts "neutral" for all 53 and scores 45.28%, "exactly the
  share of neutral labels in the set"; recall for positive and negative is
  0 (`README.md`, Honest limitations; `evals/README.md`). The 100% on 11
  synthetic cases is circular. This is "a known limitation, deliberately
  not patched to improve the number": tuning the keyword list to those 53
  rows "would just move the circularity up one level" (`evals/README.md`).
  A replacement measured on human-verified data is issue #14.
- "The review queue catches uncertainty, not errors": a confident rule hit
  is never queued, even when wrong (the demo's "charges dismissed"
  headline is negative because it contains "court").

## Alternatives considered

- Tune `NEGATIVE_KEYWORDS` to the eval set: rejected for circularity.
- Call the AI at search time: rejected; `save_results` "must not call AI"
  (`src/processing/enrich.py`; ADR 0005).
- A trained classifier: the repo has no human-verified labeled data (the
  53 labels are LLM-produced and "not yet human-verified").
