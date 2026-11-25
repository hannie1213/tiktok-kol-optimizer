"""Outgoing email. Uses SMTP when configured, otherwise logs the message (dev)."""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("kol.mailer")

# Tests can inspect what would have been sent.
outbox: list = []


def send(to: str, subject: str, body: str):
    outbox.append({"to": to, "subject": subject, "body": body})
    del outbox[:-50]

    host = os.environ.get("SMTP_HOST")
    if not host:
        log.warning("SMTP not configured; email to %s not sent.\nSubject: %s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "no-reply@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
