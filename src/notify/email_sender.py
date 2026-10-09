"""
Email sending (SMTP SSL)
================================
Reads SMTP configuration from the environment (.env) and sends emails,
optionally with attachments (e.g. the report Excel file). Designed for
SSL/TLS on port 465.

Environment variables:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, ALERT_EMAIL
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def config_summary() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", ""),
        "port": os.getenv("SMTP_PORT", "465"),
        "user": os.getenv("SMTP_USER", ""),
        "from": os.getenv("SMTP_FROM", "") or os.getenv("SMTP_USER", ""),
        "to": os.getenv("ALERT_EMAIL", ""),
    }


def send_email(
    subject: str,
    html_body: str,
    to: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """
    Sends an email. Returns (ok, detail). Does not raise: it catches errors
    and returns them as text so they can be shown on the web.

    `config` (keys SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
    ALERT_EMAIL) replaces the environment: Settings uses it to send a test
    with the values typed in the dialog before they are saved.
    """
    def get(key: str, default: str = "") -> str:
        if config is not None:
            return str(config.get(key) or default)
        return os.getenv(key, default) or default

    host, user, password = get("SMTP_HOST"), get("SMTP_USER"), get("SMTP_PASS")
    if not (host and user and password):
        return False, "SMTP not configured (missing SMTP_HOST/USER/PASS)."
    try:
        port = int(get("SMTP_PORT", "465"))
    except ValueError:
        return False, "The SMTP port must be a number (465 or 587)."
    sender = get("SMTP_FROM") or user
    recipient = to or get("ALERT_EMAIL") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content("This email requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    for filename, data in (attachments or []):
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=filename
        )

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:  # 587 / STARTTLS or others
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
        return True, f"Email sent to {recipient}."
    except Exception as e:  # noqa: BLE001
        return False, describe_error(e, host, port)


def describe_error(exc: Exception, host: str, port: int) -> str:
    """Plain, actionable message for a failed send (never the password)."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, smtplib.SMTPAuthenticationError) or "authentication" in low \
            or "username and password not accepted" in low:
        hint = (" Gmail needs an app password (myaccount.google.com/apppasswords), not your "
                "normal password." if "gmail" in host.lower() else
                " Check the user and password.")
        return f"The mail server rejected the user or password.{hint}"
    if isinstance(exc, ssl.SSLError) or "wrong version number" in low:
        return (f"Secure connection to {host}:{port} failed. Use port 465 for SSL or 587 for "
                "STARTTLS.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address. Check “Send alerts to”."
    if isinstance(exc, (TimeoutError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return (f"Couldn't connect to {host}:{port} ({' '.join(text.split())[:120]}). Check the "
                "server name and port: 465 uses SSL, 587 uses STARTTLS.")
    return f"Error sending: {' '.join(text.split())[:200]}"
