"""
Notification channel dispatcher (7.4)
================================
`send_via(db, channel_name, text, alert_id=None)` resolves the channel
module (telegram/slack/ntfy), sends through it and ALWAYS records the
result in `notify_log` -- both the "Test" buttons of /settings and the
future severity dispatchers (7.1) go through here, so both share the
same history.

No abstract base class (ponytail): each channel module exposes
`is_configured()` / `send(text, ...)` via duck typing (same shape already
used by email_sender), so a name -> module dict is enough for the
dispatch -- no need for a hierarchy or dynamic registry.
"""
from notify import email_sender, ntfy, slack, telegram

_CHANNELS = {"telegram": telegram, "slack": slack, "ntfy": ntfy}
# Public allowlist: the /settings/notify-test route validates against this
# (a single source of truth, review 7.4 NIT#5).
VALID_CHANNELS = tuple(_CHANNELS)

# Shown wherever a channel is saved (Settings, setup wizard): routing
# lives in Settings → Notifications and alerts, and a configured channel receives nothing unless a
# severity is routed to it. Static HTML.
ROUTING_NOTE = ("Which alerts go to which channel is set in <a href='/settings#routing'>Settings → "
                "Alert routing</a>; by default warnings go to email.")


def send_via(db, channel_name: str, text: str, alert_id: int | None = None) -> tuple[bool, str]:
    """Sends `text` through the `channel_name` channel and records it in
    notify_log (success, HTTP failure, unconfigured channel or unknown
    channel). Never raises."""
    module = _CHANNELS.get(channel_name)
    if module is None:
        detail = f"Unknown channel: {channel_name}"
        db.add_notify_log(channel=channel_name, ok=False, error=detail, alert_id=alert_id)
        return False, detail

    # design: telegram is the only channel whose configuration lives in the
    # DB (the chat_id, see brief 7.4) instead of just the environment --
    # that's why it's the only one that receives an extra kwarg here;
    # slack/ntfy are configured 100% via environment variables and don't
    # need it. If a future channel needed something similar, promote this
    # to a small channel->extra mapping.
    extra = {"chat_id": db.get_setting("telegram_chat_id", "")} if channel_name == "telegram" else {}

    # Unconfigured channel: feedback to the caller but WITHOUT a row in
    # notify_log -- every Test click on an empty channel would push real
    # deliveries out of the "last 10" window (review 7.4 #3).
    if not module.is_configured(**extra):
        return False, "not configured"

    ok, detail = module.send(text, **extra)
    db.add_notify_log(channel=channel_name, ok=ok, error=("" if ok else detail), alert_id=alert_id)
    # Settings shows "Working" / "Failing: <reason>" from the last real result.
    from common import integration_health
    integration_health.record_current(db, channel_name, ok, detail)
    return ok, detail


def channel_status(db) -> dict[str, bool]:
    """{channel: configured} using each module's own is_configured() --
    the same checks the senders use, so every page (Settings, setup
    wizard, onboarding) agrees on what "configured" means. Email = SMTP
    host + user + password; Telegram = bot token + chat id; Slack =
    webhook URL; ntfy = NTFY_URL or NTFY_TOPIC."""
    return {
        "email": email_sender.is_configured(),
        "telegram": telegram.is_configured(chat_id=db.get_setting("telegram_chat_id", "")),
        "slack": slack.is_configured(),
        "ntfy": ntfy.is_configured(),
    }


def routed_severities(db) -> list[str]:
    """Severities whose routed channel (alert_routing) can actually deliver.
    'digest' is a daily email, so it counts when email is configured;
    'none' never does."""
    status = channel_status(db)
    status["digest"] = status["email"]
    return [sev for sev, ch in db.get_routing().items() if status.get(ch)]
