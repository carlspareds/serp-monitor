"""Rules from src/processing/classifier.py: positive, negative, force-neutral
and unknown -> neutral."""
from conftest import DEMO_POSITIVE_DOMAINS

from processing.classifier import classify_result, is_force_neutral


def test_positive_domain_wins():
    result = {"domain": "jordanashworth.example",
              "url": "https://jordanashworth.example/about",
              "title": "Fraud allegation", "description": "lawsuit"}
    # Even though the title has negative words, the positive domain wins.
    assert classify_result(result, positive_domains=DEMO_POSITIVE_DOMAINS) == "positive"


def test_positive_subdomain_matches():
    result = {"domain": "blog.jordanashworth.example",
              "url": "https://blog.jordanashworth.example/post",
              "title": "", "description": ""}
    assert classify_result(result, positive_domains=DEMO_POSITIVE_DOMAINS) == "positive"


def test_static_positive_list_is_empty_by_default():
    """Multi-subject phase 1: positive domains are per subject (DB rows); the global
    config list ships empty, so the fictional demo domains are NOT positive for every
    subject anymore."""
    from config.positive_domains import POSITIVE_DOMAINS, merged_with

    assert POSITIVE_DOMAINS == []
    assert merged_with(["Own.example"]) == ["own.example"]
    result = {"domain": "jordanashworth.example",
              "url": "https://jordanashworth.example/about", "title": "", "description": ""}
    assert classify_result(result) == "neutral"


def test_negative_keyword_in_title():
    result = {"domain": "news.example", "url": "https://news.example/x",
              "title": "Jordan Ashworth arrested", "description": ""}
    assert classify_result(result) == "negative"


def test_negative_domain_blacklist():
    result = {"domain": "scam.example", "url": "https://scam.example/y",
              "title": "Nothing special", "description": ""}
    assert classify_result(result, negative_domains=["scam.example"]) == "negative"


def test_force_neutral_overrides_negative_keyword():
    result = {"domain": "company-information.service.gov.uk",
              "url": "https://company-information.service.gov.uk/officers/123",
              "title": "Court filing officer record", "description": "alleged"}
    assert is_force_neutral(result) is True
    assert classify_result(result) == "neutral"


def test_unknown_domain_defaults_to_neutral():
    result = {"domain": "random-directory.example", "url": "https://random-directory.example/z",
              "title": "Jordan Ashworth profile", "description": "General bio."}
    assert classify_result(result) == "neutral"
