"""Structured JSON logging (9.2).

stdlib `logging` with a custom JSON Formatter — structlog is NOT
installed (ponytail: stdlib first; ~15 lines cover the case). Output
goes to stdout, which journald already collects (`journalctl --user -u
serp-monitor -o json` makes it queryable).

The codebase's legacy print() calls are out of scope for 9.2 and get
converted as each module is migrated. # design: don't convert them as a
side effect.
"""
import json
import logging
import sys
from datetime import UTC, datetime

from common.redact import redact, redact_obj, secret_values

# Standard LogRecord keys that are NOT "extra" from the caller.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED:
                out[key] = value
        if record.exc_text:
            out["exc"] = record.exc_text
        elif record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        # Last line of defence: the serialized record goes through redact()
        # too (an extra= value of a type the filter doesn't walk, say).
        return redact(json.dumps(out, ensure_ascii=False, default=str))


class RedactingFilter(logging.Filter):
    """Removes secrets (env secret values and key-like patterns, see
    common.redact) from every record before any handler formats it: the
    message, its %-args (structure kept -- uvicorn's access formatter unpacks
    them), string `extra=` fields and the traceback text. Always returns
    True: it rewrites records, it never drops them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            secrets = secret_values()
            if isinstance(record.msg, str):
                record.msg = redact(record.msg, secrets)
            if isinstance(record.args, (tuple, dict)):
                record.args = redact_obj(record.args, secrets)
            for key, value in list(vars(record).items()):
                if key not in _RESERVED and isinstance(value, (str, list, tuple, dict)):
                    setattr(record, key, redact_obj(value, secrets))
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text, secrets)
        except Exception:  # noqa: BLE001,S110 -- a log filter must never raise
            pass
        return True


_FILTER = RedactingFilter()
# uvicorn configures its own handlers (propagate=False): filter those too.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler")


def install_redaction() -> None:
    """Attach the redacting filter to every handler of the root logger and
    of uvicorn's loggers (idempotent). Called by setup() and again at app
    startup, after uvicorn has installed its handlers."""
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in _THIRD_PARTY_LOGGERS)]
    for lg in loggers:
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        for h in lg.handlers:
            if _FILTER not in h.filters:
                h.addFilter(_FILTER)


def setup() -> None:
    """Installs the JSON formatter on the root logger (idempotent) and the
    secret-redacting filter on every handler."""
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_json_setup", False):
            install_redaction()
            return  # already installed (test reload / double import)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._json_setup = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    install_redaction()
