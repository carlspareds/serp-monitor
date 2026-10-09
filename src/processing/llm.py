"""
LLM provider selection + the OpenAI-compatible client
=====================================================
processing/ai_classifier.py funnels every LLM call through `_call_llm`;
this module decides WHICH provider that seam talks to and implements the
OpenAI-compatible one. The Gemini client itself stays in ai_classifier
(`_call_gemini`, unchanged -- tests stub it by name there).

Environment
-----------
LLM_PROVIDER      'gemini' | 'openai' (case-insensitive). Unset/empty ->
                  auto: 'gemini' if GEMINI_API_KEY is set, else 'openai' if
                  OPENAI_BASE_URL or OPENAI_API_KEY is set, else no provider.
GEMINI_API_KEY    Gemini key (required for provider gemini).
GEMINI_MODEL      Gemini model, default GEMINI_DEFAULT_MODEL.
OPENAI_BASE_URL   Full API base INCLUDING the version segment, e.g.
                  https://api.openai.com/v1 (default), http://localhost:11434/v1
                  (Ollama), http://localhost:1234/v1 (LM Studio). A trailing
                  '/' or a pasted '/chat/completions' suffix is stripped; no
                  '/v1' is ever added -- what you give is what is used.
OPENAI_API_KEY    Sent as 'Authorization: Bearer ...' when set. Optional for
                  non-official base URLs (local servers usually need none);
                  required for the official api.openai.com endpoint.
OPENAI_MODEL      Model name. Defaults to OPENAI_DEFAULT_MODEL ONLY when the
                  base URL is the official OpenAI one; any other server
                  (Ollama, LM Studio, vLLM, OpenRouter...) has no sensible
                  universal default, so it is REQUIRED there -- without it the
                  provider reports "not configured" instead of guessing.
"""

import contextlib
import contextvars
import hashlib
import json
import os
import uuid
from urllib.parse import urlparse

import requests

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash-lite"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
PROVIDERS = ("gemini", "openai")
_OFFICIAL_OPENAI_HOST = "api.openai.com"
_TIMEOUT = 30

NOT_CONFIGURED = "No AI provider configured."


# Candidate settings for a "Test before saving" (Settings → AI provider): the
# values typed in the dialog replace the environment for the calls made
# inside config_override(), in this context only (never os.environ, so
# concurrent requests and the scheduler keep using the saved config).
_OVERRIDE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_config_override", default=None)


def _env(name: str) -> str:
    override = _OVERRIDE.get()
    if override is not None and name in override:
        return (override[name] or "").strip()
    return (os.getenv(name) or "").strip()


@contextlib.contextmanager
def config_override(values: dict):
    """Every provider lookup inside the block reads `values` (env var name ->
    value) instead of the environment for the names it contains."""
    token = _OVERRIDE.set(dict(values))
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def overridden() -> bool:
    """True inside config_override(): a test of unsaved values."""
    return _OVERRIDE.get() is not None


def provider() -> str:
    """Selected provider: 'gemini', 'openai', '' (none) or the raw unknown
    value of LLM_PROVIDER (never treated as configured)."""
    explicit = _env("LLM_PROVIDER").lower()
    if explicit:
        return explicit
    if _env("GEMINI_API_KEY"):
        return "gemini"
    if _env("OPENAI_BASE_URL") or _env("OPENAI_API_KEY"):
        return "openai"
    return ""


def form_provider() -> str:
    """Provider a settings form should preselect: the current one (also when
    it is selected but failing), else the one whose settings are present.
    An unknown LLM_PROVIDER value never preselects a provider the user did
    not configure -- that is how an OpenAI-compatible setup used to be
    saved back as Gemini."""
    p = provider()
    if p in PROVIDERS:
        return p
    if _env("GEMINI_API_KEY"):
        return "gemini"
    if _env("OPENAI_BASE_URL") or _env("OPENAI_API_KEY") or _env("OPENAI_MODEL"):
        return "openai"
    return "gemini"


PROVIDER_NEEDS = {
    "gemini": "Gemini needs an API key. Paste it in the Gemini key field or choose "
              "OpenAI-compatible.",
    "openai": "OpenAI-compatible needs a base URL or an API key. Fill them in or "
              "choose Gemini.",
}


def resolve_form_provider(chosen: str, *, gemini_submitted: bool,
                          openai_submitted: bool) -> tuple[str, str]:
    """Provider to save for an AI settings form -> (provider, error).

    Never saves a provider that has nothing to run on: when the chosen one
    has no settings (none submitted now, none saved) but the other one's
    fields were filled in this submission, the other one is used. When the
    chosen one has nothing and nothing else was filled in, the error says
    so and the caller writes nothing. `chosen` must be '' or in PROVIDERS;
    '' infers from what was filled in ('' when ambiguous/empty)."""
    has = {
        "gemini": gemini_submitted or bool(_env("GEMINI_API_KEY")),
        "openai": openai_submitted or bool(_env("OPENAI_BASE_URL") or _env("OPENAI_API_KEY")),
    }
    submitted = {"gemini": gemini_submitted, "openai": openai_submitted}
    if not chosen:
        only = [p for p in PROVIDERS if submitted[p]]
        return (only[0] if len(only) == 1 else ""), ""
    if has[chosen]:
        return chosen, ""
    other = "openai" if chosen == "gemini" else "gemini"
    if submitted[other]:
        return other, ""
    return "", PROVIDER_NEEDS[chosen]


def openai_base_url() -> str:
    """OPENAI_BASE_URL normalised: trailing '/' and a pasted
    '/chat/completions' suffix removed. Default: the official endpoint."""
    base = _env("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    return base


def _is_official_openai(base: str) -> bool:
    return (urlparse(base).hostname or "").lower() == _OFFICIAL_OPENAI_HOST


def openai_model() -> str:
    """OPENAI_MODEL, or OPENAI_DEFAULT_MODEL for the official endpoint only;
    '' (= not configured) for any other server without an explicit model."""
    explicit = _env("OPENAI_MODEL")
    if explicit:
        return explicit
    return OPENAI_DEFAULT_MODEL if _is_official_openai(openai_base_url()) else ""


def gemini_model() -> str:
    return _env("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL


def model() -> str:
    """Model of the selected provider ('' when none/unknown)."""
    p = provider()
    if p == "gemini":
        return gemini_model()
    if p == "openai":
        return openai_model()
    return ""


def not_configured_reason() -> str:
    """'' when configured; otherwise a provider-neutral, secret-free reason."""
    p = provider()
    if not p:
        return NOT_CONFIGURED
    if p == "gemini":
        if not _env("GEMINI_API_KEY"):
            return f"{NOT_CONFIGURED} LLM_PROVIDER=gemini but GEMINI_API_KEY is not set."
        return ""
    if p == "openai":
        base = openai_base_url()
        if _is_official_openai(base) and not _env("OPENAI_API_KEY"):
            return f"{NOT_CONFIGURED} LLM_PROVIDER=openai with the official endpoint needs OPENAI_API_KEY."
        if not openai_model():
            return f"{NOT_CONFIGURED} OPENAI_MODEL is required for a custom OPENAI_BASE_URL."
        return ""
    return f"{NOT_CONFIGURED} Unknown LLM_PROVIDER '{p[:40]}' (use: gemini, openai)."


def is_configured() -> bool:
    return not not_configured_reason()


def _host(url: str) -> str:
    """Host[:port] only -- never scheme/path/userinfo (UI-safe)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return f"{host}:{parsed.port}" if host and parsed.port else host


def provider_info() -> dict:
    """For UI display: {provider, model, base_url (host only), configured}.
    Never contains a secret."""
    p = provider()
    if p == "gemini":
        base = "generativelanguage.googleapis.com"
    elif p == "openai":
        base = _host(openai_base_url())
    else:
        base = ""
    return {"provider": p, "model": model(), "base_url": base, "configured": is_configured()}


def cache_tag() -> str:
    """Suffix appended to every verdict-cache prompt_version so verdicts from
    one provider/model are never served for another.

    LEGACY: Gemini with its default model returns '' -- every row cached
    before multi-provider support was produced by exactly that setup, so the
    existing cache stays valid (no mass re-classification/quota burn on
    upgrade). Same with NO provider selected: nothing can be computed then,
    but previously cached verdicts stay readable exactly as before. Every
    other combination gets '@<provider>-<sha1[:8]>' of provider+model(+base
    URL for openai: the same model name on two servers -- e.g. 'llama3' on
    Ollama vs LM Studio -- may be different weights)."""
    p, m = provider(), model()
    if not p or (p == "gemini" and m == GEMINI_DEFAULT_MODEL):
        return ""
    ident = f"{p}:{m}"
    if p == "openai":
        ident += f"@{openai_base_url()}"
    return f"@{p}-{hashlib.sha1(ident.encode()).hexdigest()[:8]}"


def _secrets() -> list[str]:
    out = []
    key = _env("OPENAI_API_KEY")
    if key:
        out.append(key)
    password = urlparse(openai_base_url()).password
    if password:
        out.append(password)
    return out


def _scrub(text: str) -> str:
    for secret in _secrets():
        text = text.replace(secret, "<secret>")
    return text


# Lowercased fragments of urllib3/socket error text -> plain reason. Checked
# in order; the first match wins.
_CONNECTION_REASONS = (
    (("name or service not known", "nodename nor servname", "getaddrinfo failed",
      "failed to resolve", "nameresolutionerror", "temporary failure in name resolution",
      "no address associated"), "host name not found (DNS lookup failed)"),
    (("connection refused", "actively refused"), "connection refused"),
    (("network is unreachable", "no route to host"), "network unreachable"),
    (("connection reset", "remotedisconnected", "connection aborted"),
     "the server closed the connection"),
)


def describe_request_error(exc: Exception, url: str, hint: str) -> str | None:
    """Plain, one-line message for a network-level `requests` failure:
    "Couldn't reach <host>: <reason>. <hint>". Only the host[:port] is
    shown (never the path, query string or userinfo, so neither a Gemini
    key in the URL nor a base-URL password can leak), and the raw
    exception text ("HTTPConnectionPool(...): Max retries exceeded...") is
    never included. Returns None for anything that isn't a network error
    (the caller keeps its generic message)."""
    rex = requests.exceptions
    host = _host(url) or "the AI provider"
    if isinstance(exc, (rex.InvalidURL, rex.MissingSchema, rex.InvalidSchema)):
        return f"The base URL is not valid ({type(exc).__name__}). {hint}"
    if isinstance(exc, rex.SSLError):
        reason = "SSL/TLS handshake failed (wrong http/https, or an untrusted certificate)"
    elif isinstance(exc, rex.ConnectTimeout):
        reason = f"connection timed out after {_TIMEOUT}s"
    elif isinstance(exc, rex.Timeout):
        reason = f"no response within {_TIMEOUT}s"
    elif isinstance(exc, rex.ConnectionError):
        text = str(exc).lower()
        reason = next((r for keys, r in _CONNECTION_REASONS if any(k in text for k in keys)),
                      "connection failed")
    else:
        return None
    return f"Couldn't reach {host}: {reason}. {hint}"


# --------------------------------------------------------------------- #
# OpenCode routing session (x-opencode-session)
# --------------------------------------------------------------------- #
# OpenCode (opencode.ai, e.g. the "OpenCode Go" plan) refuses requests that
# don't carry an `x-opencode-session` header (HTTP 400 MissingSessionID):
# it routes all calls of one session to the same backend. The id is opaque:
# a UUID per logical conversation when the caller opens a session_scope()
# (assistant: one per chat session; searches: one per run), otherwise one
# UUID for the whole process. Never sent to any other host.
_PROCESS_SESSION_ID = str(uuid.uuid4())
_SESSION_NAMESPACE = uuid.UUID("6f1d3c52-0e4b-4c1e-9a57-3d0b8c1e2f41")
_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_session_id", default=None)
OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode(base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def session_id() -> str:
    """The current routing session id (scoped, else per process)."""
    return _SESSION_ID.get() or _PROCESS_SESSION_ID


@contextlib.contextmanager
def session_scope(key: str = ""):
    """Every LLM call inside the block shares one session id: a stable UUID
    derived from `key` (the raw key is never sent), or a fresh random one
    when `key` is empty."""
    sid = str(uuid.uuid5(_SESSION_NAMESPACE, key)) if key else str(uuid.uuid4())
    token = _SESSION_ID.set(sid)
    try:
        yield sid
    finally:
        _SESSION_ID.reset(token)


def routing_headers(base_url: str) -> dict:
    """Extra request headers some OpenAI-compatible hosts need ({} for
    every other host)."""
    if is_opencode(base_url):
        return {OPENCODE_SESSION_HEADER: session_id()}
    return {}


# --------------------------------------------------------------------- #
# Provider HTTP errors -> plain messages
# --------------------------------------------------------------------- #
_SETTINGS_HINT = "Check Settings → AI provider."


def _error_fields(body: str) -> tuple[str, str]:
    """(type/status/code, message) from a provider's JSON error body, or
    ('', '') when the body isn't JSON. Handles OpenAI-style
    {"error": {"type", "message"}}, Gemini {"error": {"status", "message"}},
    {"error": "text"} and {"message": "text"}."""
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    err = data.get("error", data)
    if isinstance(err, str):
        return "", err
    if not isinstance(err, dict):
        return "", ""
    kind = err.get("type") or err.get("status") or err.get("code") or ""
    message = err.get("message") or err.get("detail") or ""
    return str(kind), str(message) if not isinstance(message, (dict, list)) else ""


def describe_http_error(status: int, body: str = "", base_url: str = "") -> str:
    """Plain, one-line message for an HTTP error from the AI provider. Never
    contains the raw response body (no JSON, no echoed key): it is shown in
    Settings, in assistant replies, in run notes and in logs."""
    kind, message = _error_fields(body)
    text = f"{kind} {message} {'' if message else (body or '')[:400]}".lower()
    opencode = is_opencode(base_url)
    name = "OpenCode" if opencode else "The AI provider"
    if "subscription" in text and status in (401, 402, 403):
        if opencode:
            return ("OpenCode rejected the request: an active OpenCode Go subscription is required "
                    "for this model. Check your plan or choose another model/provider in "
                    "Settings → AI provider.")
        return ("The AI provider rejected the request: an active subscription is required for "
                f"this model. Check your plan or choose another model. {_SETTINGS_HINT}")
    if "missingsessionid" in text or "missing x-opencode-session" in text:
        return (f"{name} couldn't route the request (missing session id, HTTP {status}). "
                "Update SERP Monitor or choose another provider.")
    if status == 401 or "api key not valid" in text or "invalid api key" in text             or "incorrect api key" in text or "api_key_invalid" in text:
        return f"The AI provider rejected the API key (HTTP {status}). {_SETTINGS_HINT}"
    if status == 402 or "insufficient_quota" in text or "credit" in text and status in (400, 403):
        return (f"The AI provider refused the request: no credit or quota left on this account "
                f"(HTTP {status}). {_SETTINGS_HINT}")
    if status == 403:
        return (f"The AI provider refused access (HTTP 403). Check the API key, its permissions "
                f"and your plan. {_SETTINGS_HINT}")
    if status == 404 or "model_not_found" in text or ("model" in text and "not found" in text):
        return (f"The AI provider doesn't know this model or URL (HTTP {status}). "
                f"{_SETTINGS_HINT}")
    if status == 429:
        return ("The AI provider is rate limiting requests or the quota is used up (HTTP 429). "
                "Try again in a minute.")
    if status >= 500:
        return f"The AI provider had a server error (HTTP {status}). Try again later."
    return f"The AI provider returned an error (HTTP {status})."


# --------------------------------------------------------------------- #
# Output token budgets (reasoning models)
# --------------------------------------------------------------------- #
# Reasoning models (DeepSeek, o-series, Gemini 2.5 "thinking", Qwen3...)
# spend part of the output budget on hidden reasoning BEFORE writing the
# answer. With a tiny max_tokens (the old 8-60 for one-word labels) they
# use it all up thinking and return EMPTY content with finish_reason=length.
# Every caller therefore asks for at least one of these floors; a
# non-reasoning model simply stops early, so a large budget costs nothing.
SHORT_OUTPUT_TOKENS = 1024   # labels/JSON: classify, severity, same-name, ping
LONG_OUTPUT_TOKENS = 2048    # prose: executive summary, assistant replies
# A finish_reason=length answer is retried ONCE with a doubled budget, never
# above this cap.
MAX_OUTPUT_TOKENS_CAP = 8192

EMPTY_ANSWER = ("The model returned no answer (it may be a reasoning model that needs "
                "more output tokens).")
# Shown next to an EMPTY_ANSWER failure (Settings → AI provider, assistant).
REASONING_HINT = ("Reasoning models are supported; if this persists, try again or pick a "
                  "non-reasoning model.")


def reasoning_hint(reason: str) -> str:
    """REASONING_HINT when `reason` is the empty-answer failure, else ''."""
    return REASONING_HINT if "returned no answer" in (reason or "") else ""


def output_budget(requested: int, floor: int = SHORT_OUTPUT_TOKENS) -> int:
    """Reasoning-safe max output tokens: never below `floor`, never above
    MAX_OUTPUT_TOKENS_CAP."""
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = 0
    return min(max(requested, floor), MAX_OUTPUT_TOKENS_CAP)


def retry_budget(current: int) -> int:
    """Budget for the one retry after finish_reason=length (0 = no retry:
    already at the cap)."""
    if current >= MAX_OUTPUT_TOKENS_CAP:
        return 0
    return min(max(current, 1) * 2, MAX_OUTPUT_TOKENS_CAP)


# --------------------------------------------------------------------- #
# OpenAI-compatible request adaptation (remembered per server+model)
# --------------------------------------------------------------------- #
# * Some servers/models reject `max_tokens` and want `max_completion_tokens`
#   (OpenAI o-series/gpt-5 and a few proxies): on a 400/422 that names
#   either parameter the request is resent with `max_completion_tokens`.
# * `reasoning_effort` (default "low", env AI_REASONING_EFFORT=low|medium|
#   high|off) keeps reasoning models from burning the budget on thinking.
#   Servers that don't know it may answer 400/422: the request is resent
#   without it, and if that works the server+model is remembered as not
#   supporting it.
# Both memories live for the process (reset_adaptations() for tests).
_TOKEN_PARAM: dict[str, str] = {}
_NO_REASONING_EFFORT: set[str] = set()
_EFFORTS = ("low", "medium", "high", "minimal")


def reasoning_effort() -> str:
    """'' (don't send) or the reasoning_effort value to send."""
    raw = _env("AI_REASONING_EFFORT").lower()
    if raw in ("off", "none", "0", "false", "no"):
        return ""
    return raw if raw in _EFFORTS else "low"


def reset_adaptations() -> None:
    _TOKEN_PARAM.clear()
    _NO_REASONING_EFFORT.clear()


def adaptation_ident(base_url: str, model_name: str) -> str:
    return f"{(base_url or '').rstrip('/')}|{model_name}"


def _rejects_max_tokens(text: str) -> bool:
    """A 400 body saying the server wants max_completion_tokens (and not,
    say, "max_tokens must be <= 8192")."""
    if "max_completion_tokens" in text:
        return True
    return "max_tokens" in text and any(
        w in text for w in ("unsupported", "not supported", "unrecognized", "unknown", "not allowed"))


def post_chat_completion(url: str, payload: dict, headers: dict, timeout: int, *,
                         base_url: str, model_name: str, tools_sent: bool = False):
    """POST an OpenAI-compatible chat completion, adapting the request to
    what the server accepts (see above). `payload` carries `max_tokens`;
    returns the final `requests.Response` (the caller handles its status).
    Network exceptions propagate unchanged."""
    ident = adaptation_ident(base_url, model_name)
    body = dict(payload)
    tokens = body.pop("max_tokens", None)
    if tokens is not None:
        body[_TOKEN_PARAM.get(ident, "max_tokens")] = tokens
    effort = reasoning_effort()
    if effort and ident not in _NO_REASONING_EFFORT:
        body["reasoning_effort"] = effort
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    for _attempt in range(2):
        if resp.status_code not in (400, 422):
            return resp
        text = (resp.text or "").lower()
        if tokens is not None and "max_tokens" in body and _rejects_max_tokens(text):
            body["max_completion_tokens"] = body.pop("max_tokens")
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                _TOKEN_PARAM[ident] = "max_completion_tokens"
            continue
        if "reasoning_effort" in body and not (
                tools_sent and ("tool" in text or "function" in text)
                and "reasoning" not in text):
            body.pop("reasoning_effort")
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                _NO_REASONING_EFFORT.add(ident)
            continue
        return resp
    return resp


def message_text(message) -> str:
    """Text content of an OpenAI-style message (string, None, or a list of
    content parts); hidden reasoning fields are ignored."""
    content = (message or {}).get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(p.get("text") or "" for p in content
                          if isinstance(p, dict) and p.get("type", "text") == "text")
    return (content or "").strip() if isinstance(content, str) else ""


def call_openai(prompt: str, max_tokens: int = SHORT_OUTPUT_TOKENS, temperature: float = 0.0,
                lower: bool = True) -> tuple[bool, str]:
    """POST {base}/chat/completions. Same (ok, text) contract and error
    shapes as ai_classifier._call_gemini. The bearer key travels only in a
    header (requests' exceptions/reprs don't include headers), and every
    returned string is additionally scrubbed of the key and any URL
    password -- some servers echo the key back in their 401 body.

    Empty content is a FAILURE (EMPTY_ANSWER), never ok=True with nothing.
    finish_reason=length is retried once with a doubled budget (capped);
    the answer then returned has no truncation marker (it would end up in
    PDFs and the UI)."""
    reason = not_configured_reason() if provider() == "openai" else ""
    m = openai_model()
    if reason or not m:
        return False, reason or NOT_CONFIGURED
    base = openai_base_url()
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json", **routing_headers(base)}
    key = _env("OPENAI_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    budget = max(1, int(max_tokens or 1))
    retried = False
    try:
        while True:
            payload = {
                "model": m,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": budget,
                "temperature": temperature,
            }
            resp = post_chat_completion(url, payload, headers, _TIMEOUT,
                                        base_url=base, model_name=m)
            if resp.status_code >= 400:
                return False, _scrub(describe_http_error(resp.status_code, resp.text or "", base))
            data = resp.json()
            choice = data["choices"][0]
            text = message_text(choice.get("message"))
            if choice.get("finish_reason") == "length":
                bigger = retry_budget(budget)
                if bigger and not retried:
                    budget, retried = bigger, True
                    continue
            if not text:
                return False, EMPTY_ANSWER
            return True, (text.lower() if lower else text)
    except requests.exceptions.RequestException as e:
        message = describe_request_error(e, url, "Check the base URL.")
        return False, _scrub(message or f"Error: {str(e)[:300]}")
    except Exception as e:  # noqa: BLE001
        return False, _scrub(f"Error: {str(e)[:300]}")
