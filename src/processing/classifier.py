"""
SEO result classification module
================================
Classifies each result as 'positive', 'negative' or 'neutral'.

Rules (in priority order):
  1. If the domain is in the list of known POSITIVE domains -> positive.
  2. If the title/description/URL contains negative words -> negative.
  3. Any other case -> neutral.

Positive domains live in `config/positive_domains.py` so they can be
extended easily without touching the logic.
"""

from typing import Any

from config.positive_domains import (
    FORCE_NEUTRAL_URL_FRAGMENTS,
    NEGATIVE_KEYWORDS,
    POSITIVE_DOMAINS,
)


def is_force_neutral(result: dict[str, Any]) -> bool:
    """Public record / sources that must not be marked negative due to a namesake."""
    url = (result.get("url") or "").lower()
    domain = (result.get("domain") or "").lower()
    haystack = f"{url} {domain}"
    for frag in FORCE_NEUTRAL_URL_FRAGMENTS:
        if frag.lower() in haystack:
            return True
    return False


def _domain_matches(domain: str, positives: list[str]) -> bool:
    """
    True if `domain` is exactly a positive domain or one of its subdomains.
    E.g.: 'blog.example.com' matches 'example.com'.
    """
    domain = (domain or "").lower()
    for pos in positives:
        pos = pos.lower()
        if domain == pos or domain.endswith("." + pos):
            return True
    return False


def classify_with_confidence(
    result: dict[str, Any],
    positive_domains: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    negative_domains: list[str] | None = None,
) -> dict[str, Any]:
    """
    Like classify_result, but in addition to the verdict it exposes
    confidence and classified_by (2.1, confidence score + human review
    queue): all deterministic rules (positive/black domain, keyword,
    force-neutral) return confidence=1.0 -- they're rule-certain. Only the
    "neutral by default" case (no rule decides) returns confidence=0.5:
    that's today's real gray zone and the natural candidate for /review.

    Priority: positive domain > force-neutral > negative domain (blacklist)
    > negative word > neutral by default.

    The lists can be injected (e.g. extended from the web); if they're None
    the base config lists are used (the base blacklist is empty).
    """
    positives = positive_domains if positive_domains is not None else POSITIVE_DOMAINS
    negatives = negative_keywords if negative_keywords is not None else NEGATIVE_KEYWORDS
    neg_domains = negative_domains if negative_domains is not None else []

    domain = result.get("domain", "") or ""
    url = (result.get("url", "") or "").lower()
    title = (result.get("title", "") or "").lower()
    description = (result.get("description", "") or "").lower()

    # 1. Positive domain (exact domain/subdomain match, with fallback to URL)
    if _domain_matches(domain, positives) or any(p.lower() in url for p in positives):
        return {"classification": "positive", "confidence": 1.0, "classified_by": "rule"}

    # 1b. Public record / forced neutral (e.g. UK Companies House)
    if is_force_neutral(result):
        return {"classification": "neutral", "confidence": 1.0, "classified_by": "rule"}

    # 2. Domain on the blacklist
    if _domain_matches(domain, neg_domains) or any(n.lower() in url for n in neg_domains):
        return {"classification": "negative", "confidence": 1.0, "classified_by": "rule"}

    # 3. Negative words in title / description / URL
    haystack = f"{title} {description} {url}"
    for keyword in negatives:
        if keyword.lower() in haystack:
            return {"classification": "negative", "confidence": 1.0, "classified_by": "rule"}

    # 4. Neutral by default -- no rule decided, real gray zone.
    return {"classification": "neutral", "confidence": 0.5, "classified_by": "rule"}


def classify_result(
    result: dict[str, Any],
    positive_domains: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    negative_domains: list[str] | None = None,
) -> str:
    """
    Classifies a single normalized result; returns only the label
    ('positive'/'negative'/'neutral'). Thin wrapper over
    classify_with_confidence (single source of the rule logic) so as not to
    break callers that only need the verdict (e.g. the 9.1 rule tests).
    """
    return classify_with_confidence(
        result, positive_domains, negative_keywords, negative_domains
    )["classification"]


def classify_batch(
    results: list[dict[str, Any]],
    positive_domains: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    negative_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Classifies a list of results (adds 'classification', and since 2.1
    also 'confidence'/'classified_by' -- see classify_with_confidence)."""
    for result in results:
        info = classify_with_confidence(
            result, positive_domains, negative_keywords, negative_domains
        )
        result["classification"] = info["classification"]
        result["confidence"] = info["confidence"]
        result["classified_by"] = info["classified_by"]
    return results


def get_summary(results: list[dict[str, Any]]) -> dict[str, int]:
    """Summary of counts per classification."""
    summary = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
    for r in results:
        classification = r.get("classification", "neutral")
        summary[classification] = summary.get(classification, 0) + 1
        summary["total"] += 1
    return summary
