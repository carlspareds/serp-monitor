"""
Sending via Telegram (Bot API sendMessage)
================================
Requires TELEGRAM_BOT_TOKEN in the environment (.env). The chat_id is NOT a
process secret -- it is stored as a DB setting (key telegram_chat_id, see
/settings) and the caller passes it in (notify.send_via).

parse_mode HTML instead of MarkdownV2: MarkdownV2 forces escaping ~14
special characters in a fragile way (one stray unescaped character breaks
the whole send); HTML only requires escaping &, < and > -- exactly what
html.escape() does.

Environment variables: TELEGRAM_BOT_TOKEN
"""
import html
import json
import os
import re

from notify.base import post

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def is_configured(chat_id: str = "") -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(chat_id)


def send(text: str, chat_id: str = "", token: str | None = None) -> tuple[bool, str]:
    """Sends `text` (escaped for HTML) to the given chat_id. Never raises:
    returns (ok, detail) same as email_sender.send_email. The Bot API
    responds with 4xx on failures (not 200+ok:false), so the HTTP status
    code is enough as the verdict. `token` defaults to TELEGRAM_BOT_TOKEN
    (Settings passes the one typed in the dialog to test it before saving)."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return False, "not configured"

    payload = {"chat_id": chat_id, "text": html.escape(text), "parse_mode": "HTML"}
    ok, detail = post(API_URL.format(token=token), secret=token, json=payload)
    return ok, (detail if ok else describe_error(detail))


_SETTINGS = "Settings → Notifications → Telegram"


def describe_error(detail: str) -> str:
    """Bot API failure ("HTTP 400: {json}") -> an actionable, plain message.
    Anything that isn't an HTTP error (network, not configured) is returned
    unchanged."""
    m = re.match(r"HTTP (\d{3}): ?(.*)", detail or "", re.DOTALL)
    if not m:
        return detail
    status, body = int(m.group(1)), m.group(2)
    try:
        data = json.loads(body)
        description = str(data.get("description") or "") if isinstance(data, dict) else ""
    except ValueError:
        description = body
    low = description.lower()
    if "chat not found" in low:
        return ("Telegram couldn't find this chat. Open your bot in Telegram, press Start (or send "
                f"any message), then check the chat id ({_SETTINGS}).")
    if status == 401 or "unauthorized" in low:
        return ("Telegram rejected the bot token (HTTP 401). Copy the token again from @BotFather "
                f"and save it in {_SETTINGS}.")
    if "blocked" in low:
        return ("The bot was blocked by this Telegram user (HTTP 403). Open the bot in Telegram, "
                "unblock it and press Start, then send the test again.")
    if status == 403:
        return ("Telegram doesn't let the bot write to this chat (HTTP 403). Add the bot to the "
                "group or channel (or press Start in a private chat), then try again.")
    if status == 429:
        return "Telegram is rate limiting the bot (HTTP 429). Try again in a minute."
    if status == 400 and "chat_id" in low:
        return f"The chat id isn't valid (HTTP 400). Check it in {_SETTINGS}."
    plain = " ".join(description.split())[:160] if description and not description.startswith("{") else ""
    return f"Telegram refused the message (HTTP {status})" + (f": {plain}" if plain else ".")


def _chat_name(chat: dict) -> str:
    if chat.get("title"):
        return str(chat["title"])
    name = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip()
    if chat.get("username"):
        name = f"{name} (@{chat['username']})" if name else f"@{chat['username']}"
    return name or "Unnamed chat"


def recent_chats(token: str) -> tuple[bool, list[dict] | str]:
    """Chats that recently wrote to the bot (Bot API getUpdates), newest
    first: [{"id", "name", "type"}]. On failure (False, plain message).
    Never raises, and the token never appears in what is returned (it is
    part of the URL, which requests' exceptions include)."""
    import requests

    if not token:
        return False, "Paste the bot token first (step 1)."
    try:
        resp = requests.get(UPDATES_URL.format(token=token), timeout=15,
                            params={"allowed_updates": json.dumps(
                                ["message", "channel_post", "my_chat_member"])})
    except Exception as exc:  # noqa: BLE001
        reason = " ".join(str(exc).replace(token, "<secret>").split())[:160]
        return False, f"Couldn't reach Telegram ({reason}). Check the server's internet connection."
    if resp.status_code == 409:
        return False, ("This bot sends its updates to a webhook, so Telegram can't list its chats "
                       "here. Remove the webhook (call deleteWebhook for this bot, e.g. from the "
                       "tool that set it) or create a new bot with @BotFather, then try again.")
    if resp.status_code == 401 or resp.status_code == 404:
        return False, ("Telegram rejected the bot token. Copy it again from @BotFather (the whole "
                       "line, like 123456789:AA…) and try again.")
    if resp.status_code != 200:
        return False, f"Telegram returned an error (HTTP {resp.status_code}). Try again in a minute."
    try:
        updates = resp.json().get("result") or []
    except ValueError:
        return False, "Telegram sent an answer that couldn't be read. Try again in a minute."
    chats: dict = {}
    for update in reversed(updates):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") if isinstance(update, dict) else None
            if isinstance(chat, dict) and chat.get("id") is not None and chat["id"] not in chats:
                chats[chat["id"]] = {"id": str(chat["id"]), "name": _chat_name(chat),
                                     "type": str(chat.get("type") or "private")}
    return True, list(chats.values())
