"""HTTP-level protections, all in-process and dependency-free.

* ``SecurityHeadersMiddleware`` -- CSP and the other response headers on
  every response, ``Strict-Transport-Security`` over HTTPS only, the
  FORCE_HTTPS redirect, ``Secure`` on every cookie set over HTTPS, and
  ``Cache-Control: no-store`` on dynamic pages (a private dashboard should
  not linger in shared caches or the back/forward cache after sign-out).
* ``BodySizeLimitMiddleware`` -- rejects request bodies over MAX_BODY_BYTES
  (default 1 MiB; the app has no file uploads) with 413, whether or not the
  client sent a Content-Length.
* ``RateLimitMiddleware`` -- in-memory token buckets per client IP and per
  signed-in account for the expensive or brute-forceable routes (login,
  first-admin setup, password changes, API, assistant, search start, report
  creation, integration tests) plus a generous global per-IP bucket. 429
  with Retry-After. Configurable per rule with RATE_LIMIT_<RULE>=N/SECONDS.
* Bot checks for the anonymous forms (login, first-admin setup): a honeypot
  field and a signed "form rendered at" timestamp with a minimum
  time-to-submit (BOT_MIN_SUBMIT_SECONDS).
* ``FailureTracker`` -- consecutive-failure lockout with exponential
  back-off (login and first-admin setup).
* Trusted proxies: dashboard.main wraps the app in uvicorn's
  ProxyHeadersMiddleware for the addresses in TRUSTED_PROXIES (or
  FORWARDED_ALLOW_IPS), so X-Forwarded-For/-Proto are believed only from
  the reverse proxy itself.

Everything is process-local: this is a single-process, single-file-SQLite
app, and a restart resetting counters is acceptable. Run one worker.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

_TRUE = {"1", "true", "yes", "on"}


def env_flag(name: str, env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(name) or "").strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def is_loopback(host: str | None) -> bool:
    """127.0.0.0/8, ::1 or "localhost"."""
    host = (host or "").strip().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_ip(scope_or_request) -> str:
    scope = scope_or_request.scope if isinstance(scope_or_request, Request) else scope_or_request
    client = scope.get("client")
    return client[0] if client else ""


def request_is_https(scope_or_request) -> bool:
    """Scheme as seen by the app (after trusted-proxy rewriting)."""
    scope = scope_or_request.scope if isinstance(scope_or_request, Request) else scope_or_request
    return scope.get("scheme") in ("https", "wss")


# --------------------------------------------------------------------- #
# Security headers, HTTPS
# --------------------------------------------------------------------- #
# 'unsafe-inline' for STYLES only: the server-rendered HTML still carries
# style="" attributes (layout tweaks), which CSP can't allow by nonce. No
# inline <script>, no inline event handlers, no javascript: URLs -- scripts
# come from /static only (see SECURITY.md, item 18).
CSP = ("default-src 'self'; "
       "script-src 'self'; "
       "style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; "
       "font-src 'self'; "
       "connect-src 'self'; "
       "object-src 'none'; "
       "frame-ancestors 'none'; "
       "base-uri 'self'; "
       "form-action 'self'")
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                      "interest-cohort=()")
HSTS = "max-age=31536000; includeSubDomains"

STATIC_HEADERS: tuple[tuple[str, str], ...] = (
    ("content-security-policy", CSP),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "strict-origin-when-cross-origin"),
    ("permissions-policy", PERMISSIONS_POLICY),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("x-frame-options", "DENY"),
    ("x-robots-tag", "noindex, nofollow"),
)


def force_https() -> bool:
    return env_flag("FORCE_HTTPS")


def _add_secure(cookie: bytes) -> bytes:
    if re.search(rb";\s*secure\b", cookie, re.IGNORECASE):
        return cookie
    return cookie + b"; Secure"


class SecurityHeadersMiddleware:
    """Pure ASGI. Outermost layer: it also sees the error responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = request_is_https(scope)
        path = scope.get("path", "")
        if force_https() and not https and path != "/health":
            req = Request(scope)
            target = str(req.url.replace(scheme="https"))
            resp = RedirectResponse(target, status_code=308)
            await self._send_with_headers(resp, scope, receive, send, https=False, path=path)
            return
        await self._run(scope, receive, send, https, path)

    async def _send_with_headers(self, resp, scope, receive, send, https, path):
        async def wrapped(message):
            await send(self._decorate(message, https, path))
        await resp(scope, receive, wrapped)

    async def _run(self, scope, receive, send, https, path):
        async def wrapped(message):
            await send(self._decorate(message, https, path))
        await self.app(scope, receive, wrapped)

    @staticmethod
    def _decorate(message, https: bool, path: str):
        if message["type"] != "http.response.start":
            return message
        headers = list(message.get("headers") or [])
        present = {k.lower() for k, _ in headers}
        for name, value in STATIC_HEADERS:
            if name.encode() not in present:
                headers.append((name.encode(), value.encode()))
        if https and b"strict-transport-security" not in present:
            headers.append((b"strict-transport-security", HSTS.encode()))
        if not path.startswith("/static/") and b"cache-control" not in present:
            headers.append((b"cache-control", b"no-store"))
        if https:
            headers = [(k, _add_secure(v)) if k.lower() == b"set-cookie" else (k, v)
                       for k, v in headers]
        return {**message, "headers": headers}


# --------------------------------------------------------------------- #
# Request body size
# --------------------------------------------------------------------- #
DEFAULT_MAX_BODY = 1024 * 1024


def max_body_bytes() -> int:
    return max(1024, _env_int("MAX_BODY_BYTES", DEFAULT_MAX_BODY))


class BodySizeLimitMiddleware:
    """413 for a declared Content-Length over the limit; a streamed body
    that grows past it is cut off with 413 as well."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = max_body_bytes()
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await _too_large(scope, receive, send)
                    return
        received = 0
        too_large = False
        answered = False

        async def counting_receive():
            # Past the limit the app sees a client disconnect: body parsing
            # fails before any endpoint code runs (nothing half-written),
            # and whatever error the app answers is replaced by a 413 below.
            # (Raising from here doesn't work: Starlette's http middlewares
            # read the body in a task group and would wrap the exception.)
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal answered
            if not too_large:
                await send(message)
                return
            if message["type"] == "http.response.start" and not answered:
                answered = True
                await _too_large(scope, receive, send)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not answered:
            await _too_large(scope, receive, send)


async def _too_large(scope, receive, send):
    resp = PlainTextResponse("Request body too large.", status_code=413)
    await resp(scope, receive, send)


# --------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    name: str
    limit: int          # requests ...
    period: float       # ... per this many seconds (bucket refill)
    per: str            # "ip" | "account" | "both"
    match: Callable[[str, str], bool]   # (method, path) -> bool


_POST = {"POST", "PUT", "PATCH", "DELETE"}
_INTEGRATION_TEST = re.compile(r"^/settings/(?:[a-z]+-test|integration/[a-z]+/test|"
                               r"integration/telegram/chats)$")


def _rules() -> list[Rule]:
    base = [
        Rule("login", 10, 60, "ip", lambda m, p: m == "POST" and p == "/login"),
        Rule("setup", 10, 60, "ip", lambda m, p: m in _POST and p.startswith("/setup/")),
        Rule("password", 10, 300, "both",
             lambda m, p: m in _POST and p in ("/users/password", "/users/add")),
        Rule("api", 300, 60, "ip", lambda m, p: p.startswith("/api/v1/")),
        Rule("assistant", 20, 60, "both",
             lambda m, p: m in _POST and p in ("/assistant/chat", "/assistant/confirm")),
        Rule("search", 10, 60, "both",
             lambda m, p: m in _POST and p in ("/search", "/search_multi")),
        Rule("reports", 10, 60, "both",
             lambda m, p: (m in _POST and p in ("/reports/create", "/export/run"))
             or p in ("/export/pdf", "/backup/db", "/backup/csv", "/audit/export.csv")),
        Rule("integration_test", 10, 60, "both",
             lambda m, p: m in _POST and bool(_INTEGRATION_TEST.match(p))),
        Rule("global", 1200, 60, "ip", lambda m, p: not p.startswith("/static/")),
    ]
    out = []
    for r in base:
        raw = (os.getenv(f"RATE_LIMIT_{r.name.upper()}") or "").strip()
        if raw:
            try:
                n, _, secs = raw.partition("/")
                r = Rule(r.name, max(1, int(n)), max(1.0, float(secs or 60)), r.per, r.match)
            except ValueError:
                pass
        out.append(r)
    return out


def rate_limit_enabled() -> bool:
    return (os.getenv("RATE_LIMIT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


class TokenBuckets:
    """{key: (tokens, last_refill)} with a hard size bound."""

    MAX_KEYS = 50_000

    def __init__(self):
        self._b: dict[tuple, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: tuple, limit: int, period: float, now: float | None = None) -> float:
        """Consume one token; 0.0 if allowed, else seconds until one is back."""
        now = time.monotonic() if now is None else now
        rate = limit / period
        with self._lock:
            tokens, last = self._b.get(key, (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._b[key] = (tokens - 1.0, now)
                return 0.0
            self._b[key] = (tokens, now)
            if len(self._b) > self.MAX_KEYS:
                self._b.clear()  # crude memory bound against key spraying
            return (1.0 - tokens) / rate

    def clear(self) -> None:
        with self._lock:
            self._b.clear()


class RateLimitMiddleware:
    def __init__(self, app, account_of: Callable[[Request], str] | None = None):
        self.app = app
        self.buckets = TokenBuckets()
        self.account_of = account_of

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not rate_limit_enabled():
            await self.app(scope, receive, send)
            return
        method, path = scope.get("method", "GET"), scope.get("path", "")
        ip = client_ip(scope)
        account = None
        wait = 0.0
        for rule in _rules():
            if not rule.match(method, path):
                continue
            keys = []
            if rule.per in ("ip", "both"):
                keys.append((rule.name, "ip", ip))
            if rule.per in ("account", "both"):
                if account is None:
                    account = self._account(scope)
                if account:
                    keys.append((rule.name, "user", account))
            for key in keys:
                wait = max(wait, self.buckets.take(key, rule.limit, rule.period))
        if wait > 0:
            retry = str(max(1, math.ceil(wait)))
            if path.startswith(("/api/", "/assistant/", "/jobs/", "/job/")):
                resp = JSONResponse({"detail": "Too many requests. Try again later."},
                                    status_code=429, headers={"Retry-After": retry})
            else:
                resp = PlainTextResponse("Too many requests. Try again in a moment.",
                                         status_code=429, headers={"Retry-After": retry})
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _account(self, scope) -> str:
        if self.account_of is None:
            return ""
        try:
            return self.account_of(Request(scope)) or ""
        except Exception:  # noqa: BLE001 -- never fail a request over the limiter
            return ""


# --------------------------------------------------------------------- #
# Bot checks for anonymous forms
# --------------------------------------------------------------------- #
HONEYPOT_FIELD = "website"
TIMESTAMP_FIELD = "form_ts"
_FORM_KEY = secrets.token_bytes(32)    # per process: a restart invalidates open forms
_FORM_MAX_AGE = 12 * 3600


def min_submit_seconds() -> float:
    return max(0.0, _env_float("BOT_MIN_SUBMIT_SECONDS", 1.0))


def form_token(now: float | None = None) -> str:
    ts = str(int(time.time() if now is None else now))
    sig = hmac.new(_FORM_KEY, ts.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def bot_fields_html() -> str:
    """Hidden honeypot + signed render time, for an anonymous form."""
    return (f"<div class='hp' aria-hidden='true'><label>Leave this empty"
            f"<input type='text' name='{HONEYPOT_FIELD}' tabindex='-1' autocomplete='off'>"
            f"</label></div><input type='hidden' name='{TIMESTAMP_FIELD}' value='{form_token()}'>")


def bot_check(honeypot: str, token: str, now: float | None = None) -> bool:
    """True when the submission looks human: empty honeypot and, unless
    BOT_MIN_SUBMIT_SECONDS is 0, a valid token at least that old."""
    if (honeypot or "").strip():
        return False
    min_secs = min_submit_seconds()
    if min_secs <= 0:
        return True
    ts, _, sig = (token or "").partition(".")
    if not ts.isdigit():
        return False
    expected = hmac.new(_FORM_KEY, ts.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        return False
    age = (time.time() if now is None else now) - int(ts)
    return min_secs <= age <= _FORM_MAX_AGE


# --------------------------------------------------------------------- #
# Lockout with exponential back-off
# --------------------------------------------------------------------- #
class FailureTracker:
    """After `max_failures` consecutive failures a key is locked for
    `base_seconds`, doubling with each further lockout up to `max_seconds`.
    A success clears the key."""

    MAX_KEYS = 10_000

    def __init__(self, max_failures: int = 5, base_seconds: float = 60, max_seconds: float = 3600):
        self.max_failures = max_failures
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        # key -> [failures since last lockout, lockouts so far, locked_until]
        self._d: dict[tuple, list[float]] = {}
        self._lock = threading.Lock()

    def locked(self, key: tuple, now: float | None = None) -> float:
        """Seconds left on the lockout (0 when not locked)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._d.get(key)
            if not entry or entry[2] <= now:
                return 0.0
            return entry[2] - now

    def failure(self, key: tuple, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._d) >= self.MAX_KEYS:
                self._d.clear()
            entry = self._d.setdefault(key, [0, 0, 0.0])
            entry[0] += 1
            if entry[0] >= self.max_failures:
                entry[1] += 1
                entry[0] = 0
                entry[2] = now + min(self.max_seconds, self.base_seconds * 2 ** (entry[1] - 1))

    def success(self, key: tuple) -> None:
        with self._lock:
            self._d.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key) -> bool:
        return key in self._d
