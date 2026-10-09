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
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup() -> None:
    """Installs the JSON formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_json_setup", False):
            return  # already installed (test reload / double import)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._json_setup = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
