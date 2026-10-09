"""
AI classification (Google Gemini or any OpenAI-compatible API)
==============================================================
Uses an LLM to classify ambiguous results as 'positive' / 'negative' /
'neutral' regarding the monitored subject's reputation. It's optional: with
no provider configured, it isn't used.

Provider selection (LLM_PROVIDER / GEMINI_* / OPENAI_*) and the
OpenAI-compatible client live in processing/llm.py; the Gemini client
(_call_gemini) stays here. Every call goes through the _call_llm seam.

Default Gemini model: gemini-2.5-flash-lite (generous free quota).
"""

import hashlib
import json
import os

import requests

from database.models import DEFAULT_SUBJECT_QUERY, url_hash
from processing import ai_status, llm

_DEFAULT_MODEL = llm.GEMINI_DEFAULT_MODEL
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _model() -> str:
    """Model of the ACTIVE provider (recorded in the verdict cache's `model`
    column and shown by ping). Identical to before for Gemini."""
    return llm.model()


def _gemini_model() -> str:
    return llm.gemini_model()


def is_configured() -> bool:
    return llm.is_configured()


def provider_info() -> dict:
    """{provider, model, base_url (host only), configured} -- for the UI."""
    return llm.provider_info()


def subject_name() -> str:
    """Name of the monitored subject (brand, person, product) as it appears
    in the prompts: the ACTIVE subject's tracked query (multi-subject phase 1 --
    Database.current_subject(), which resolves for_subject -> the request
    ContextVar -> subject 1, and for subject 1 falls back to the legacy
    report_query setting) when a Database is injected (see set_cache_db), else
    SUBJECT_QUERY / the built-in default.

    The classify cache version hashes this name, so AI verdicts are cached per
    subject without any extra key. Background work that doesn't inherit the
    request's context must wrap its AI calls in database.models.subject_scope(sid):
    the injected Database is the unscoped singleton, it only learns the subject
    through the ContextVar."""
    if _db is not None:
        try:
            value = _db.current_subject().get("query", "")
            if value:
                return value
        except Exception:  # noqa: BLE001, S110 -- a broken DB must not break a prompt
            pass
    return os.getenv("SUBJECT_QUERY") or DEFAULT_SUBJECT_QUERY

_VALID = {"positive", "negative", "neutral"}

# Fallback confidence (2.1): used when no valid JSON arrives and we have to
# fall back to single-word parsing, or when the JSON doesn't carry
# 'confidence'. Single value -- used by _parse_classify_json and
# _parse_classify_word.
_FALLBACK_CONFIDENCE = 0.6

# Versioned prompts (2.5): the 3 per-URL (cacheable) verdicts live in
# constants with an explicit version. Bumping the version (e.g. V1 -> V2)
# invalidates the cache naturally: the PK of ai_verdict_cache includes
# prompt_version.
PROMPT_CLASSIFY_V1 = (
    "You are an online reputation analyst. Classify the following Google search "
    "result about '{subject}' as favorable, harmful or irrelevant/neutral for "
    "its reputation.\n"
    "Answer with ONLY one word: positive, negative or neutral.\n\n"
    "Title: {title}\nURL: {url}\nDescription: {description}\n\nClassification:"
)
# V2 (2.1, confidence score): asks for JSON with classification + confidence
# (0-1) instead of a single word -- feeds the human review queue (/review)
# with the AI's real certainty level. Bumping the version invalidates the
# cache naturally (see PROMPT_VERSIONS / cached_verdict above).
PROMPT_CLASSIFY_V2 = (
    "You are an online reputation analyst. Classify the following Google search "
    "result about '{subject}' as favorable, harmful or irrelevant/neutral for "
    "its reputation.\n"
    "Respond with ONLY a JSON object (no markdown, no extra text) in this exact "
    'shape: {{"classification": "positive"|"negative"|"neutral", '
    '"confidence": <number between 0 and 1>}}.\n\n'
    "Title: {title}\nURL: {url}\nDescription: {description}\n\nJSON:"
)
# Default short bio of the monitored subject (2.3, settings.subject_context):
# used by processing/enrich.py -- the ONLY context-fallback point
# (arbitration H18/ponytail: is_same_person/is_same_person_batch further
# below do NOT have their own fallback, they assume the caller already
# resolved `context`).
def default_subject_context() -> str:
    """Fallback bio of the subject when settings.subject_context is empty."""
    return subject_name()
# V2 (2.3): the subject is no longer hardcoded in the prompt text -- it
# comes from {context} (resolved by the caller, see DEFAULT_SUBJECT_CONTEXT
# above).
# 'unsure' instead of 'maybe' (ponytail): the DB schema (same_person) uses
# yes/no/unsure -- asking the model for it directly avoids a separate
# normalization layer. Cache version: see _same_person_version (H8).
PROMPT_SAME_PERSON_V2 = (
    "The monitored subject is: {context}\n"
    "Is this result about THAT person (not a namesake)? "
    "Answer ONLY: yes, no, or unsure.\n\n"
    "Title: {title}\nURL: {url}\nDescription: {description}\n"
)
# Real batch (2.3, Gemini budget): a single call for up to N numbered
# results -- see is_same_person_batch. Each item is cached individually per
# URL under the same verdict_type/version as is_same_person (V2), so a
# later per-URL is_same_person hit reuses what the batch already decided.
PROMPT_SAME_PERSON_BATCH_V1 = (
    "The monitored subject is: {context}\n"
    "For EACH numbered result below, decide if it is about THAT person (not "
    "a namesake). Respond with ONLY a JSON array (no markdown, no extra "
    "text), one object per result, in this exact shape: "
    '[{{"i": <number>, "same": "yes"|"no"|"unsure"}}, ...].\n\n'
    "{items}\n"
)
# Closed topic taxonomy (2.2, deep dive §2.2): used by PROMPT_ANALYZE_NEG_V2
# and _parse_analyze_negative_json -- a topic the model returns outside this
# set (or empty) is clamped to 'other', an arbitrary value is never
# persisted in the `topic` column (CLOSED taxonomy).
TOPICS = {"legal", "financial", "personal", "labor", "product", "opinion", "other"}
# V2 (2.2): EXPLICIT severity rubric (domain authority x content gravity x
# prominence, with the extremes anchored -- deep dive §2.2) + CLOSED topic
# taxonomy (TOPICS) instead of V1's ad-hoc category (legal|fraud|press|
# defamation|other). JSON instead of lines (M4, same pattern as
# PROMPT_CLASSIFY_V2/PROMPT_AI_ANSWER_V1) -- defensive parsing via
# _parse_analyze_negative_json, more robust than V1's line-by-line parsing.
# Also returns 'action' and 'same' (part of the verdict, unrelated to
# persistence in `search_results` -- see processing/severity.py).
PROMPT_ANALYZE_NEG_V2 = (
    "You are an online reputation analyst. Analyze this NEGATIVE Google "
    "result about '{subject}'.\n"
    "Rate its SEVERITY on a 1-5 scale using this rubric: severity = domain "
    "authority x content gravity x prominence. Anchors: 1 = a minor mention "
    "on a minor/low-authority site (e.g. a forum comment, a small blog); "
    "5 = a grave accusation (legal, fraud, criminal) published by a "
    "high-authority outlet (a major newspaper, an official registry, a "
    "national broadcaster). Use the full 1-5 range for intermediate cases "
    "(e.g. a moderate complaint on a mid-authority site is a 2 or 3).\n"
    "Classify its TOPIC as EXACTLY ONE of: legal, financial, personal, "
    "labor, product, opinion, other.\n"
    "Also suggest a brief, concrete SEO mitigation ACTION, and say whether "
    "this result is about THAT specific person or a namesake (SAME: yes, "
    "no, or unsure).\n"
    "Respond with ONLY a JSON object (no markdown, no extra text) in this "
    'exact shape: {{"severity": <integer 1-5>, "topic": "legal"|'
    '"financial"|"personal"|"labor"|"product"|"opinion"|"other", '
    '"action": <string>, "same": "yes"|"no"|"unsure"}}.\n\n'
    "Title: {title}\nURL: {url}\nDescription: {description}\n\nJSON:"
)
# V3 (live test): same labels + a short plain REASON (shown in "Why it's
# here" / explain_result). The cache key stays "v2" on purpose: the label
# and confidence mean exactly the same, the reason is additive, and keeping
# the key means verdicts cached before this change (no reason) are still
# served -- explain_row then simply shows no AI reason for them.
PROMPT_CLASSIFY_V3 = (
    "You are an online reputation analyst. Classify the following Google search "
    "result about '{subject}' as favorable, harmful or irrelevant/neutral for "
    "its reputation.\n"
    "Respond with ONLY a JSON object (no markdown, no extra text) in this exact "
    'shape: {{"label": "positive"|"negative"|"neutral", '
    '"confidence": <number between 0 and 1>, '
    '"reason": <one short plain sentence, at most 140 characters, saying why>}}.\n\n'
    "Title: {title}\nURL: {url}\nDescription: {description}\n\nJSON:"
)
_MAX_REASON_CHARS = 140

PROMPT_VERSIONS = {
    "classify": "v2",
    # v2 (2.2): PROMPT_ANALYZE_NEG_V2 -- bumping the version alone
    # invalidates the cache of negatives analyzed with prompt V1
    # (line-by-line, without a severity rubric or closed topic taxonomy).
    "analyze_negative": "v2",
    # NEITHER same_person NOR ai_answer_sentiment live here (H8, 2.3 /
    # review A1): their cache version includes a hash of the context
    # (subject's bio) and is computed in _same_person_version /
    # _ai_answer_version -- changing the bio in /settings invalidates the
    # cache on its own, without a manual bump.
}

# ---------------------------------------------------------------------- #
# Per-URL AI verdict cache (2.5)
# ---------------------------------------------------------------------- #
# processing/ must not depend on dashboard/: instead of importing the
# Database built in dashboard.deps, this receives the instance by injection
# (set_cache_db, called once from dashboard.deps when building `db`).
# url_hash() IS imported directly from database.models: it's a pure
# stateless function and database/ is a layer below processing/ (no cycle).
_db = None


def set_cache_db(db) -> None:
    """Injects the Database to use for the verdict cache (2.5) -- and for
    the AI provider status (processing/ai_status.py)."""
    global _db
    ai_status.set_db(db)
    _db = db


def _is_valid_verdict(verdict) -> bool:
    """An empty/failed verdict must not poison the cache.

    Intent: a PARTIALLY empty dict (e.g. only category) IS cached -- it's a
    real Gemini response with imperfect parsing, not a network failure;
    calling again wouldn't improve it. Only the fully-empty case ('' or a
    dict with no value at all) is considered a failure and recomputed next
    time.
    """
    if isinstance(verdict, dict):
        return any(verdict.values())
    return bool(verdict)


def cached_verdict(url: str, verdict_type: str, compute_fn, prompt_version: str | None = None):
    """Cache-or-compute an AI verdict per URL.

    Without an injected DB (_db None: standalone script, direct import
    without going through dashboard.deps) or without a URL, it always
    computes without caching -- same behavior as before 2.5.

    prompt_version (H8, 2.3): if passed explicitly, it wins over
    PROMPT_VERSIONS[verdict_type] -- used by is_same_person, whose cache
    version depends on the context (subject's bio), not on a fixed
    constant.
    """
    if _db is None or not url:
        return compute_fn()
    version = prompt_version if prompt_version is not None else _subject_version(verdict_type)
    version = _cache_version(version)
    h = url_hash(url)
    cached = _db.get_cached_verdict(h, verdict_type, version)
    if cached is not None:
        return cached["verdict"]
    verdict = compute_fn()
    if _is_valid_verdict(verdict):
        _db.set_cached_verdict(h, verdict_type, version, {"verdict": verdict}, model=_model())
    return verdict


def _subject_version(verdict_type: str) -> str:
    """Cache version for the per-URL verdicts whose prompt embeds the subject
    name: changing the subject invalidates the cache naturally."""
    digest = hashlib.sha1(subject_name().encode()).hexdigest()[:8]
    return f"{PROMPT_VERSIONS[verdict_type]}-{digest}"


def _same_person_version(context: str) -> str:
    """same_person cache PK (H8, 2.3): includes a short hash of the context
    (subject's bio) in addition to the prompt version -- changing the bio
    in /settings naturally invalidates what's cached for non-human URLs
    (same idea as bumping PROMPT_VERSIONS, but automatic by content instead
    of manual)."""
    return f"v2-{hashlib.sha1(context.encode()).hexdigest()[:8]}"


def _cache_version(version: str) -> str:
    """Final cache PK version: the prompt/subject version + the provider/model
    tag (llm.cache_tag) -- switching provider or model never serves a verdict
    another model produced. Gemini on its default model keeps the bare legacy
    version, so the pre-existing cache stays valid."""
    return f"{version}{llm.cache_tag()}"


def _call_gemini(prompt: str, max_tokens: int = llm.SHORT_OUTPUT_TOKENS, temperature: float = 0.0,
                  lower: bool = True) -> tuple[bool, str]:
    """`lower` (A5 review H1): the classifiers (one-word/short-JSON
    messages -- classify_with_ai, same_person) rely on comparing lowercased
    text against their labels ('positive'/'negative'/...), so the default
    stays True -- ZERO behavior change for them. PROSE consumers (the PDF's
    AI executive summary) need the original casing/Markdown intact -- they
    pass lower=False explicitly (see executive_summary)."""
    key = llm._env("GEMINI_API_KEY")
    if not key:
        return False, llm.NOT_CONFIGURED
    url = f"{_BASE}/{_gemini_model()}:generateContent?key={key}"
    budget = max(1, int(max_tokens or 1))
    retried = False
    try:
        while True:
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": budget},
            }
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code >= 400:
                return False, llm.describe_http_error(resp.status_code, resp.text or "").replace(
                    key, "<secret>")
            data = resp.json()
            candidate = (data.get("candidates") or [{}])[0] or {}
            parts = (candidate.get("content") or {}).get("parts") or []
            # Thinking models may add thought parts; only the answer counts.
            text = "".join(p.get("text") or "" for p in parts
                           if isinstance(p, dict) and not p.get("thought")).strip()
            # MAX_TOKENS (a thinking model can spend the whole budget before
            # answering): one retry with a doubled, capped budget. The text
            # returned never carries a truncation marker -- it ends up in
            # PDFs and the UI.
            if candidate.get("finishReason") == "MAX_TOKENS":
                bigger = llm.retry_budget(budget)
                if bigger and not retried:
                    budget, retried = bigger, True
                    continue
            if not text:
                return False, llm.EMPTY_ANSWER
            return True, (text.lower() if lower else text)
    except requests.exceptions.RequestException as e:
        # Network-level failure: a plain "Couldn't reach ..." line (host only,
        # so the key in the query string never appears); anything else falls
        # back to the generic message below, still redacted.
        message = llm.describe_request_error(e, url, "Check your network connection.")
        if message:
            return False, message
        return False, f"Error: {str(e)[:300]}".replace(key, "<secret>")
    except Exception as e:  # noqa: BLE001
        # Secret redaction (review, recurrence of the HIGH 7.4 finding --
        # see notify/base.py): GEMINI_API_KEY travels in `url`'s query
        # string, and requests/urllib3 exceptions (timeout, DNS, connection
        # refused...) include the full URL in their representation.
        # Without redacting, `ping()` would persist the key in plain text
        # all the way to /settings (see ai_classifier.ping, which returns
        # `out` as-is).
        return False, f"Error: {str(e)[:300]}".replace(key, "<secret>")


def _call_llm(prompt: str, max_tokens: int = llm.SHORT_OUTPUT_TOKENS, temperature: float = 0.0,
              lower: bool = True) -> tuple[bool, str]:
    """Single LLM entry point: dispatches to the selected provider (see
    processing/llm.py). Same contract as _call_gemini: (ok, text).

    The output budget is ALWAYS reasoning-safe: whatever a caller asks for
    is raised to at least llm.SHORT_OUTPUT_TOKENS (a reasoning model given
    a tiny budget spends it all thinking and answers nothing -- the old
    default of 10 broke the Settings "Test" button)."""
    reason = llm.not_configured_reason()
    if reason:
        return False, reason
    max_tokens = llm.output_budget(max_tokens)
    if llm.provider() == "openai":
        ok, out = llm.call_openai(prompt, max_tokens=max_tokens, temperature=temperature, lower=lower)
    else:
        ok, out = _call_gemini(prompt, max_tokens=max_tokens, temperature=temperature, lower=lower)
    if ok and not (out or "").strip():
        # An empty answer is never a success (reasoning models that ran out
        # of output tokens return exactly that).
        ok, out = False, llm.EMPTY_ANSWER
    # Every real call (configured provider) feeds the AI provider status in
    # Settings and the per-run failure collector (ai_status.collect_failures).
    ai_status.record(ok, "" if ok else out)
    return ok, out


_JSON_DECODER = json.JSONDecoder()


def _scan_json(text: str, marker: str):
    """Shared generator (ponytail, 2.3) for _parse_classify_json and
    _parse_same_person_batch: walks `text` and, for each occurrence of the
    `marker` character ('{' for objects, '[' for arrays), tries to decode a
    complete JSON value from there with json.JSONDecoder.raw_decode. Yields
    each successfully decoded value, in order -- the caller iterates and
    keeps the first one that meets ITS own validity criterion. Unlike a
    greedy regex (`\\{.*\\}`), this doesn't swallow stray text/objects that
    come AFTER the real value (markdown, model chatter...) -- original M4
    pattern from 2.1."""
    for i, ch in enumerate(text):
        if ch != marker:
            continue
        try:
            data, _end = _JSON_DECODER.raw_decode(text, i)
        except (json.JSONDecodeError, ValueError):
            continue
        yield data


def _parse_classify_json(text: str) -> dict:
    """Parses the JSON response from prompt V2 ({"classification":...,
    "confidence":...}). M4 (2.1): stops at the first valid object with a
    recognized 'classification' (see _scan_json). Returns {} if there's no
    valid object -- the caller then falls back to single-word parsing
    (compatible with prompt V1 / models that don't obey the format).

    M4 sub-note: confidence in (1, 100] is interpreted as a percentage
    (85 -> 0.85) before clamping to [0, 1] -- some models return confidence
    on that scale even though the prompt asks for 0-1.
    """
    for data in _scan_json(text, "{"):
        if not isinstance(data, dict):
            continue
        cls = str(data.get("classification") or data.get("label") or "").strip().lower()
        if cls not in _VALID:
            continue
        try:
            confidence = float(data.get("confidence", _FALLBACK_CONFIDENCE))
        except (TypeError, ValueError):
            confidence = _FALLBACK_CONFIDENCE
        if 1 < confidence <= 100:
            confidence = confidence / 100
        confidence = max(0.0, min(1.0, confidence))
        out = {"classification": cls, "confidence": confidence}
        reason = clean_reason(data.get("reason"))
        if reason:
            out["reason"] = reason
        return out
    return {}


def clean_reason(raw) -> str:
    """One plain line of at most _MAX_REASON_CHARS characters ('' if none)."""
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return text


def _parse_classify_word(text: str) -> dict:
    """Deterministic fallback (M3, 2.1) when no valid JSON arrives: looks
    for the 3 labels as a substring of the text. If EXACTLY one appears, it
    uses it (confidence _FALLBACK_CONFIDENCE); if 0 or MORE than one appear
    (ambiguous text -- e.g. "could be positive or negative"), there's no
    clear verdict to cache: returns {} instead of an arbitrary label (it
    used to depend on a set's iteration order, non-deterministic)."""
    found = [label for label in ("positive", "negative", "neutral") if label in text]
    if len(found) != 1:
        return {}
    return {"classification": found[0], "confidence": _FALLBACK_CONFIDENCE}


def _classify_ai_dict(record: dict) -> dict:
    """Computes (with cache 2.5, version v2) the full AI verdict:
    {"classification", "confidence"[, "classified_by"]}, or {} if it fails
    / doesn't classify. Single source for classify_with_ai (str,
    backward-compat) and classify_with_ai_confidence (dict, 2.1).

    B11: force-neutral is a deterministic RULE (not an AI verdict: it never
    calls Gemini), so it's marked classified_by='rule' despite living in
    this module -- classify_with_ai_confidence respects that value instead
    of always forcing 'ai'.
    """
    from processing.classifier import is_force_neutral

    if is_force_neutral(record):
        return {"classification": "neutral", "confidence": 1.0, "classified_by": "rule"}

    def _compute() -> dict:
        prompt = PROMPT_CLASSIFY_V3.format(
            subject=subject_name(),
            title=(record.get("title") or "")[:200],
            url=(record.get("url") or "")[:200],
            description=(record.get("description") or "")[:300],
        )
        # lower=False keeps the reason's casing; labels are lowercased by
        # the parsers.
        ok, out = _call_llm(prompt, max_tokens=llm.output_budget(60), lower=False)
        if not ok:
            return {}
        return _parse_classify_json(out) or _parse_classify_word(out.lower())

    result = cached_verdict(record.get("url") or "", "classify", _compute)
    if not isinstance(result, dict):
        # B8: legacy cache (entries from before 2.1, or any manual
        # corruption) may have stored a plain string instead of a dict --
        # doesn't crash, normalizes it as if it were the single-word
        # fallback.
        return {"classification": result, "confidence": _FALLBACK_CONFIDENCE} if result else {}
    return result


def classify_with_ai(record: dict) -> str:
    """Returns 'positive'/'negative'/'neutral' or '' if it fails / doesn't classify."""
    return _classify_ai_dict(record).get("classification") or ""


def classify_with_ai_confidence(record: dict) -> dict:
    """Like classify_with_ai, but exposes confidence and classified_by
    (2.1, review queue) -- {"classification": "", "confidence": 0.0,
    "classified_by": "ai"} if it fails."""
    d = _classify_ai_dict(record)
    cls = d.get("classification") or ""
    out = {
        "classification": cls,
        "confidence": d.get("confidence", _FALLBACK_CONFIDENCE) if cls else 0.0,
        "classified_by": d.get("classified_by", "ai") if cls else "ai",
    }
    # Cached verdicts from before the reason existed simply have none.
    reason = clean_reason(d.get("reason")) if cls and out["classified_by"] == "ai" else ""
    if reason:
        out["reason"] = reason
    return out


def ping() -> tuple[bool, str]:
    """Tests the configured provider with a minimal request."""
    if not is_configured():
        return False, llm.not_configured_reason()
    with llm.session_scope():
        ok, out = _call_llm("Reply with only: ok", max_tokens=llm.SHORT_OUTPUT_TOKENS)
    # Success only when real text came back (_call_llm already turns an
    # empty answer into a failure; this guards any other provider path).
    if ok and not (out or "").strip():
        ok, out = False, llm.EMPTY_ANSWER
    ai_status.record(ok, "" if ok else out, force=True)
    if not ok:
        return False, out
    if llm.provider() == "gemini":
        return True, f"Connection successful (model {_model()})."
    return True, f"Connection successful ({llm.provider()}, model {_model()})."


def generate_text_result(prompt: str, max_tokens: int = 500, lower: bool = True) -> tuple[bool, str]:
    """(ok, text-or-plain-failure-reason). Like generate_text, but keeps the
    reason so a caller can say WHY there's no text."""
    if not is_configured():
        return False, llm.not_configured_reason()
    ok, out = _call_llm(prompt, max_tokens=max_tokens, temperature=0.3, lower=lower)
    if ok and not (out or "").strip():
        return False, llm.EMPTY_ANSWER
    return ok, out


def generate_text(prompt: str, max_tokens: int = 500, lower: bool = True) -> str:
    """Free-form AI text (or '' if it fails / not configured). `lower`
    (H1, A5 review): default True preserves this helper's historical
    behavior for its one-word/short-label consumers (e.g. analyze_negative
    -- ZERO change, they don't pass it). PROSE consumers (executive_summary)
    pass lower=False."""
    ok, out = generate_text_result(prompt, max_tokens=max_tokens, lower=lower)
    return out if ok else ""



def executive_summary_result(data_text: str) -> tuple[bool, str]:
    """(ok, summary) or (False, plain reason). See executive_summary."""
    prompt = (
        "You are an online reputation consultant. Using this Google monitoring data "
        f"about '{subject_name()}', write a short EXECUTIVE SUMMARY (3-5 "
        "sentences, in English, professional tone) highlighting what matters most: "
        "overall status, risks (negatives) and actionable recommendations.\n\n"
        f"DATA:\n{data_text}\n\nEXECUTIVE SUMMARY:"
    )
    return generate_text_result(prompt, max_tokens=llm.LONG_OUTPUT_TOKENS, lower=False)


def executive_summary(data_text: str) -> str:
    """Writes a brief executive summary from a data text. lower=False
    (# design, brief fix-live-payloads #3 -- sweep of PROSE consumers left
    out of the cross-cutting H1, A5 review fix): this summary is rendered
    as-is in the executive PDF (dashboard.main::export_pdf ->
    export.pdf_report.build_pdf), 3-5 sentences in English -- lowercasing
    it was pure noise for the operator."""
    ok, out = executive_summary_result(data_text)
    return out if ok else ""



# Fixed, generous max_tokens for the batch prompt (H7, 2.3): up to
# _BATCH_SIZE (10, see processing/enrich.py) {"i","same"} items fit
# comfortably in 512 tokens; a fixed constant is simpler and more
# predictable than scaling with len(records), and avoids cutting the
# response mid-way if the model adds extra text despite the prompt.
# Live test: raised to the reasoning-safe prose floor -- a reasoning model
# thinks about all 10 items before writing the array.
_SAME_PERSON_BATCH_MAX_TOKENS = llm.LONG_OUTPUT_TOKENS


def is_same_person(record: dict, context: str = "") -> str:
    """
    #4 Disambiguation: is the result about THIS subject (`context`, already
    resolved by the caller -- see DEFAULT_SUBJECT_CONTEXT/H18, the only
    fallback lives in processing/enrich.py) or another namesake?
    Returns 'yes' | 'no' | 'unsure' | '' (if it fails).
    """

    def _compute() -> str:
        prompt = PROMPT_SAME_PERSON_V2.format(
            context=context,
            title=(record.get("title") or "")[:200],
            url=(record.get("url") or "")[:200],
            description=(record.get("description") or "")[:300],
        )
        ok, out = _call_llm(prompt, max_tokens=llm.SHORT_OUTPUT_TOKENS)
        if not ok:
            return ""
        out = out.lower()
        if "no" in out and "unsure" not in out:
            return "no"
        if "yes" in out:
            return "yes"
        return "unsure"

    return cached_verdict(
        record.get("url") or "", "same_person", _compute,
        prompt_version=_same_person_version(context),
    )


def _parse_same_person_batch(text: str) -> dict[int, str]:
    """Parses the batch prompt's JSON response (array of {"i","same"}).

    H7/H9 (2.3): PARTIAL -- returns {1-based index: verdict} for ONLY the
    items the model answered well-formed; the ones it omitted, truncated,
    or returned with an unrecognizable index/value simply don't appear (the
    caller, is_same_person_batch, leaves those positions as None -- they
    get retried on the next observation of that URL, without paying an
    extra call right now). Uses the first array in the text (see
    _scan_json) that produces AT LEAST one valid item; {} if none match."""
    for data in _scan_json(text, "["):
        if not isinstance(data, list):
            continue
        by_index: dict[int, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            same = str(item.get("same", "")).strip().lower()
            if same in ("yes", "no", "unsure"):
                by_index[idx] = same
        if by_index:
            return by_index
    return {}


def is_same_person_batch(records: list[dict], context: str = "") -> list[str | None]:
    """
    #4 BATCH disambiguation (2.3, Gemini budget): a single call for up to
    ~10 `records`. ALWAYS returns a list aligned 1:1 with `records` -- each
    position is 'yes'|'no'|'unsure', or None if Gemini isn't configured,
    the call fails, or the model didn't return (or returned malformed) the
    verdict for THAT specific item (H7/H9: partial, no per-URL fallback --
    see processing/enrich.py).

    Caches each VALID verdict individually per URL, writing directly with
    set_cached_verdict (not cached_verdict-as-writer: here we already know
    the value, there's no compute_fn to avoid re-running) under the same
    version as is_same_person (_same_person_version) -- a later per-URL hit
    reuses what the batch decided.
    """
    if not records:
        return []
    if not is_configured():
        return [None] * len(records)
    items = "\n".join(
        f"{i + 1}. Title: {(r.get('title') or '')[:200]}\n"
        f"   URL: {(r.get('url') or '')[:200]}\n"
        f"   Description: {(r.get('description') or '')[:300]}"
        for i, r in enumerate(records)
    )
    prompt = PROMPT_SAME_PERSON_BATCH_V1.format(context=context, items=items)
    ok, out = _call_llm(prompt, max_tokens=_SAME_PERSON_BATCH_MAX_TOKENS)
    if not ok:
        return [None] * len(records)
    parsed = _parse_same_person_batch(out)
    version = _cache_version(_same_person_version(context))
    verdicts: list[str | None] = []
    for i, r in enumerate(records, start=1):
        verdict = parsed.get(i)
        verdicts.append(verdict)
        if verdict is not None and _db is not None:
            h = url_hash(r.get("url") or "")
            _db.set_cached_verdict(h, "same_person", version, {"verdict": verdict}, model=_model())
    return verdicts


def _parse_analyze_negative_json(text: str) -> dict:
    """Parses PROMPT_ANALYZE_NEG_V2's JSON response (2.2).

    M4 (same pattern as _parse_classify_json/_parse_ai_answer_json): stops
    at the first object that carries a 'severity' parseable to int (the
    field that anchors the whole feature -- without it the object is
    useless, it keeps looking for another one with _scan_json). Returns {}
    if no object in the text carries a valid severity (Gemini not
    configured/failed/non-JSON response -- the caller caches the
    empty-failure the same way, see _is_valid_verdict, and
    processing.severity.annotate_severity simply leaves severity/topic as
    NULL).

    DEFENSIVE parsing (brief 2.2): severity outside 1..5 -> clamp to that
    range (not discarded: a model that returns 6 or 0 is still a real
    signal, it just needs bounding). topic outside the closed taxonomy
    (TOPICS) -- or absent -- -> 'other'.

    F5 (review 2.2): `int(float(str(...)))` instead of plain `int(...)` --
    a model that answers "4.5" (float instead of int despite the prompt) no
    longer discards the WHOLE object (topic/action/same included) just for
    that; it truncates toward the lower integer (4.5 -> 4, same behavior as
    int() on a float, no rounding -- an arbitrary decision documented here,
    any truncation approach is equally defensible). "4/5" (non-numeric)
    STILL fails to parse -- float("4/5") raises ValueError, and that
    candidate is discarded as before (_scan_json tries the next object in
    the text, if any).
    """
    for data in _scan_json(text, "{"):
        if not isinstance(data, dict):
            continue
        try:
            severity = int(float(str(data.get("severity")).strip()))
        except (TypeError, ValueError):
            continue
        severity = max(1, min(5, severity))
        topic = str(data.get("topic", "")).strip().lower()
        if topic not in TOPICS:
            topic = "other"
        action = str(data.get("action", "")).strip()
        same = str(data.get("same", "")).strip().lower()
        if same not in ("yes", "no", "unsure"):
            same = ""
        # 'category' is kept as an ALIAS of `topic` (same value, same
        # closed taxonomy) -- backward-compat for callers that read
        # info.get('category') from before 2.2. NOT
        # a second taxonomy: one value, two access keys (ponytail, avoids
        # duplicating the concept).
        return {"topic": topic, "severity": severity, "action": action,
                "same": same, "category": topic}
    return {}


def analyze_negative(record: dict) -> dict:
    """
    For a negative result: severity (1-5, explicit rubric), topic (closed
    taxonomy TOPICS) and a suggested action (2.2, evolves V1 -> V2). Returns
    {'topic','category','severity','action','same'} -- all empty if it
    fails (Gemini not configured, no network in tests, non-JSON response).
    'category' is an alias of 'topic' (see _parse_analyze_negative_json).
    """

    def _compute() -> dict:
        prompt = PROMPT_ANALYZE_NEG_V2.format(
            subject=subject_name(),
            title=(record.get("title") or "")[:200],
            url=(record.get("url") or "")[:200],
            description=(record.get("description") or "")[:300],
        )
        out = generate_text(prompt, max_tokens=llm.output_budget(250))
        parsed = _parse_analyze_negative_json(out)
        if parsed:
            return parsed
        return {"category": "", "severity": "", "action": "", "same": "", "topic": ""}

    return cached_verdict(record.get("url") or "", "analyze_negative", _compute)

