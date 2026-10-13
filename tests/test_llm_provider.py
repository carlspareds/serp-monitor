"""Multi-provider LLM layer (processing/llm.py + ai_classifier._call_llm).

No real network: requests.post is always replaced by a fake. Every test
starts from a clean LLM environment (_clean_env, autouse) so the
developer's real .env can't leak a provider into the resolution matrix.
"""
import pytest
import requests

from database.models import Database
from processing import ai_classifier, llm

_LLM_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
             "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
SECRET = "sk-test-SUPERSECRET-123"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    ai_classifier.set_cache_db(None)


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content, finish="stop"):
    return FakeResp(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                       "finish_reason": finish}]})


@pytest.fixture
def capture_post(monkeypatch):
    """Replaces requests.post; returns the list of captured calls. Set
    `capture_post.response` (or an Exception) to control the outcome."""
    class Box(list):
        response = _ok("Positive")

    box = Box()

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        box.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        if isinstance(box.response, Exception):
            raise box.response
        return box.response

    monkeypatch.setattr(requests, "post", fake_post)
    return box


# --------------------------------------------------------------------- #
# Provider resolution matrix
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("env", "provider", "configured"),
    [
        ({}, "", False),
        ({"GEMINI_API_KEY": "g"}, "gemini", True),
        ({"OPENAI_API_KEY": "k"}, "openai", True),  # official default + default model
        ({"OPENAI_BASE_URL": "http://localhost:11434/v1"}, "openai", False),  # no model
        ({"OPENAI_BASE_URL": "http://localhost:11434/v1", "OPENAI_MODEL": "llama3"}, "openai", True),
        ({"OPENAI_MODEL": "gpt-4o"}, "", False),  # model alone selects nothing
        ({"GEMINI_API_KEY": "g", "OPENAI_API_KEY": "k"}, "gemini", True),  # backward compat
        ({"LLM_PROVIDER": "openai", "GEMINI_API_KEY": "g", "OPENAI_API_KEY": "k"}, "openai", True),
        ({"LLM_PROVIDER": "OpenAI ", "OPENAI_API_KEY": "k"}, "openai", True),
        ({"LLM_PROVIDER": "gemini", "OPENAI_API_KEY": "k"}, "gemini", False),  # no gemini key
        ({"LLM_PROVIDER": "openai"}, "openai", False),  # official endpoint, no key
        ({"LLM_PROVIDER": "claude", "GEMINI_API_KEY": "g"}, "claude", False),  # unknown
        ({"LLM_PROVIDER": "", "GEMINI_API_KEY": "g"}, "gemini", True),  # empty == unset
    ],
)
def test_provider_resolution_matrix(monkeypatch, env, provider, configured):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert llm.provider() == provider
    assert llm.is_configured() is configured
    assert ai_classifier.is_configured() is configured
    assert bool(llm.not_configured_reason()) is (not configured)
    if not configured:
        assert llm.not_configured_reason().startswith("No AI provider configured.")


def test_openai_model_default_only_for_official_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert llm.openai_model() == llm.OPENAI_DEFAULT_MODEL
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1/")
    assert llm.openai_model() == llm.OPENAI_DEFAULT_MODEL
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    assert llm.openai_model() == ""
    monkeypatch.setenv("OPENAI_MODEL", "meta/llama")
    assert llm.openai_model() == "meta/llama"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:11434/v1", "http://localhost:11434/v1"),
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("http://localhost:1234/v1/chat/completions", "http://localhost:1234/v1"),
        ("  https://api.openai.com/v1//  ", "https://api.openai.com/v1"),
    ],
)
def test_base_url_normalisation(monkeypatch, raw, expected):
    monkeypatch.setenv("OPENAI_BASE_URL", raw)
    assert llm.openai_base_url() == expected


def test_provider_info_shapes(monkeypatch):
    assert llm.provider_info() == {"provider": "", "model": "", "base_url": "", "configured": False}
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert ai_classifier.provider_info() == {
        "provider": "gemini", "model": "gemini-2.5-flash-lite",
        "base_url": "generativelanguage.googleapis.com", "configured": True,
    }
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://user:pw@localhost:11434/v1")
    monkeypatch.setenv("OPENAI_MODEL", "llama3")
    info = ai_classifier.provider_info()
    assert info == {"provider": "openai", "model": "llama3",
                    "base_url": "localhost:11434", "configured": True}
    assert SECRET not in str(info) and "pw" not in str(info)


# --------------------------------------------------------------------- #
# OpenAI-compatible request shape
# --------------------------------------------------------------------- #
def test_openai_request_shape_with_key(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    ok, out = ai_classifier._call_llm("hello", max_tokens=2048, temperature=0.3)
    assert (ok, out) == (True, "positive")  # lower=True default, like Gemini
    [call] = capture_post
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["json"] == {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}],
                            "max_tokens": 2048, "temperature": 0.3, "reasoning_effort": "low"}
    assert call["timeout"] == 30


def test_openai_request_without_key_sends_no_auth_header(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "llama3")
    ok, _ = ai_classifier._call_llm("hi")
    assert ok
    [call] = capture_post
    assert call["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in call["headers"]
    assert call["json"]["model"] == "llama3"


def test_openai_lower_false_keeps_casing_and_never_adds_truncation_marker(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    capture_post.response = _ok("  **Summary** Text  ", finish="length")
    ok, out = ai_classifier._call_llm("x", max_tokens=1500, lower=False)
    assert ok
    assert out == "**Summary** Text"          # no marker: it would reach PDFs/UI
    # finish_reason=length -> one retry with a doubled budget
    assert [c["json"]["max_tokens"] for c in capture_post] == [1500, 3000]


def test_executive_summary_via_openai(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    capture_post.response = _ok("Overall Status Is Stable.")
    assert ai_classifier.executive_summary("data") == "Overall Status Is Stable."


def test_classify_with_ai_via_openai(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    capture_post.response = _ok('{"classification": "negative", "confidence": 0.9}')
    rec = {"url": "https://x.example/a", "title": "t", "description": "d"}
    assert ai_classifier.classify_with_ai_confidence(rec)["classification"] == "negative"


def test_gemini_path_untouched_when_provider_gemini(monkeypatch, capture_post):
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_API_KEY", "ok")  # ignored: gemini wins (auto)
    capture_post.response = FakeResp(200, {"candidates": [
        {"content": {"parts": [{"text": "Neutral"}]}, "finishReason": "STOP"}]})
    ok, out = ai_classifier._call_llm("p", max_tokens=7)   # raised to the reasoning-safe floor
    assert (ok, out) == (True, "neutral")
    [call] = capture_post
    assert call["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                           "gemini-2.5-flash-lite:generateContent?key=gk")
    assert call["json"] == {"contents": [{"parts": [{"text": "p"}]}],
                            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024}}
    assert call["headers"] == {}


# --------------------------------------------------------------------- #
# Error paths + secret redaction
# --------------------------------------------------------------------- #
def test_openai_http_error_scrubs_echoed_key(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    capture_post.response = FakeResp(401, text=f"Incorrect API key provided: {SECRET}")
    ok, out = ai_classifier._call_llm("x")
    assert not ok
    assert out == "The AI provider rejected the API key (HTTP 401). Check Settings → AI provider."
    assert SECRET not in out


_POOL = "HTTPConnectionPool(host='llm.local', port=9): Max retries exceeded with url: /v1/chat/completions"


@pytest.mark.parametrize(
    "exc,reason",
    [
        (requests.exceptions.ConnectionError(
            f"{_POOL} (Caused by NewConnectionError('<urllib3.connection.HTTPConnection>: "
            "Failed to establish a new connection: [Errno 111] Connection refused'))"),
         "connection refused"),
        (requests.exceptions.ConnectionError(
            f"{_POOL} (Caused by NameResolutionError(\"Failed to resolve 'llm.local' "
            "([Errno -2] Name or service not known)\"))"),
         "host name not found (DNS lookup failed)"),
        (requests.exceptions.ConnectTimeout(f"{_POOL} (connect timeout=30)"),
         "connection timed out after 30s"),
        (requests.exceptions.ReadTimeout(f"timed out (Authorization: Bearer {SECRET})"),
         "no response within 30s"),
        (requests.exceptions.SSLError(f"{_POOL} (Caused by SSLError(WRONG_VERSION_NUMBER))"),
         "SSL/TLS handshake failed (wrong http/https, or an untrusted certificate)"),
        (requests.exceptions.ConnectionError("something else entirely"), "connection failed"),
    ],
)
def test_openai_network_errors_are_plain(monkeypatch, capture_post, exc, reason):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.local:9/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    capture_post.response = exc
    ok, out = ai_classifier._call_llm("x")
    assert not ok
    assert out == f"Couldn't reach llm.local:9: {reason}. Check the base URL."
    assert SECRET not in out and "HTTPConnectionPool" not in out


def test_ping_shows_the_plain_network_message(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    capture_post.response = requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=9): ... [Errno 111] Connection refused")
    assert ai_classifier.ping() == (
        False, "Couldn't reach 127.0.0.1:9: connection refused. Check the base URL.")


def test_gemini_network_error_is_plain_and_keyless(monkeypatch, capture_post):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    capture_post.response = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): Max retries "
        f"exceeded with url: /v1beta/models/m:generateContent?key={SECRET} "
        "(Caused by NameResolutionError: Failed to resolve)")
    ok, out = ai_classifier._call_llm("x")
    assert not ok
    assert out == ("Couldn't reach generativelanguage.googleapis.com: host name not found "
                   "(DNS lookup failed). Check your network connection.")


def test_gemini_http_error_stays_informative(monkeypatch, capture_post):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    capture_post.response = FakeResp(400, text="API key not valid")
    assert ai_classifier._call_llm("x") == (
        False, "The AI provider rejected the API key (HTTP 400). Check Settings → AI provider.")


@pytest.mark.parametrize(
    "resp",
    [
        FakeResp(200, {}),  # no choices
        FakeResp(200, {"choices": []}),
        FakeResp(200, {"choices": [{"message": {}}]}),
        FakeResp(200, {"choices": [{"message": {"content": None}}]}),
        FakeResp(200, None),  # non-JSON body
    ],
)
def test_openai_malformed_responses(monkeypatch, capture_post, resp):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    capture_post.response = resp
    ok, out = ai_classifier._call_llm("x")
    # A message without content is the plain "no answer" failure.
    assert ok is False and (out.startswith("Error: ") or out == llm.EMPTY_ANSWER)
    assert SECRET not in out


def test_base_url_password_scrubbed(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://bob:hunter2pw@llm.local/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    capture_post.response = requests.exceptions.ConnectionError(
        "Max retries exceeded with url: http://bob:hunter2pw@llm.local/v1/chat/completions")
    ok, out = ai_classifier._call_llm("x")
    assert not ok and "hunter2pw" not in out


def test_not_configured_messages_are_provider_neutral(capture_post):
    assert ai_classifier._call_llm("x") == (False, "No AI provider configured.")
    assert ai_classifier.ping() == (False, "No AI provider configured.")
    assert ai_classifier.generate_text("x") == ""
    assert capture_post == []


def test_ping_openai_success_and_failure(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("OPENAI_MODEL", "qwen")
    capture_post.response = _ok("ok")
    assert ai_classifier.ping() == (True, "Connection successful (openai, model qwen).")
    capture_post.response = FakeResp(500, text="boom")
    assert ai_classifier.ping() == (
        False, "The AI provider had a server error (HTTP 500). Try again later.")


def test_ping_gemini_message_unchanged(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (True, "ok"))
    assert ai_classifier.ping() == (True, "Connection successful (model gemini-2.5-flash-lite).")


def test_ping_custom_base_without_model_explains(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    ok, out = ai_classifier.ping()
    assert not ok and "OPENAI_MODEL" in out
    assert capture_post == []


# --------------------------------------------------------------------- #
# Verdict cache keyed by provider + model
# --------------------------------------------------------------------- #
def _version_for(monkeypatch, **env):
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return ai_classifier._cache_version("v2-abc")


def test_cache_version_differs_across_providers_and_models(monkeypatch):
    gemini_default = _version_for(monkeypatch, GEMINI_API_KEY="g")
    gemini_pro = _version_for(monkeypatch, GEMINI_API_KEY="g", GEMINI_MODEL="gemini-2.5-pro")
    oa_mini = _version_for(monkeypatch, OPENAI_API_KEY="k")
    oa_4o = _version_for(monkeypatch, OPENAI_API_KEY="k", OPENAI_MODEL="gpt-4o")
    ollama = _version_for(monkeypatch, OPENAI_BASE_URL="http://localhost:11434/v1", OPENAI_MODEL="llama3")
    lmstudio = _version_for(monkeypatch, OPENAI_BASE_URL="http://localhost:1234/v1", OPENAI_MODEL="llama3")
    # Legacy: Gemini on its default model keeps the bare pre-multi-provider version.
    assert gemini_default == "v2-abc"
    # No provider: legacy namespace too (cached verdicts stay readable).
    assert _version_for(monkeypatch) == "v2-abc"
    versions = [gemini_default, gemini_pro, oa_mini, oa_4o, ollama, lmstudio]
    assert len(set(versions)) == len(versions)
    assert oa_4o.startswith("v2-abc@openai-")
    # API key is not part of the identity (rotating a key keeps the cache).
    assert _version_for(monkeypatch, OPENAI_API_KEY="other", OPENAI_MODEL="gpt-4o") == oa_4o


def test_switching_provider_does_not_serve_other_models_verdict(db_path, monkeypatch, capture_post):
    ai_classifier.set_cache_db(Database(db_path=db_path))
    rec = {"url": "https://x.example/cache", "title": "t", "description": "d"}

    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda *a, **k: (True, '{"classification": "positive", "confidence": 0.9}'))
    assert ai_classifier.classify_with_ai(rec) == "positive"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    capture_post.response = _ok('{"classification": "negative", "confidence": 0.8}')
    assert ai_classifier.classify_with_ai(rec) == "negative"  # not the cached gemini verdict
    assert ai_classifier.classify_with_ai(rec) == "negative"
    assert len(capture_post) == 1  # second openai lookup is a cache hit

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert ai_classifier.classify_with_ai(rec) == "positive"  # gemini row still there


def test_same_person_batch_writes_under_provider_tagged_version(db_path, monkeypatch, capture_post):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    capture_post.response = _ok('[{"i": 1, "same": "yes"}]')
    rec = {"url": "https://x.example/sp", "title": "t", "description": "d"}
    assert ai_classifier.is_same_person_batch([rec], context="ctx") == ["yes"]
    # The per-URL path reuses the batch verdict (same tagged version): no new call.
    assert ai_classifier.is_same_person(rec, context="ctx") == "yes"
    assert len(capture_post) == 1


def test_resolve_form_provider(monkeypatch):
    for name in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
                 "OPENAI_MODEL"):
        monkeypatch.setenv(name, "")
    r = llm.resolve_form_provider
    assert r("gemini", gemini_submitted=True, openai_submitted=False) == ("gemini", "")
    assert r("gemini", gemini_submitted=False, openai_submitted=True) == ("openai", "")
    assert r("openai", gemini_submitted=True, openai_submitted=False) == ("gemini", "")
    assert r("openai", gemini_submitted=True, openai_submitted=True) == ("openai", "")
    assert r("gemini", gemini_submitted=False, openai_submitted=False) == (
        "", llm.PROVIDER_NEEDS["gemini"])
    assert llm.PROVIDER_NEEDS["gemini"] == (
        "Gemini needs an API key. Paste it in the Gemini key field or choose OpenAI-compatible.")
    assert r("openai", gemini_submitted=False, openai_submitted=False)[0] == ""
    assert r("", gemini_submitted=False, openai_submitted=True) == ("openai", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert r("gemini", gemini_submitted=False, openai_submitted=True) == ("gemini", "")
