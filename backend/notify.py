"""
Instant operator alerts through a Telegram bot (optional).

  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your chat with the bot (see docs/DEPLOY-RENDER.md)

Both unset → nothing is sent. Failures are logged and never reach the caller,
so a Telegram outage can't break a payment submission.
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger("notify")

TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def enabled() -> bool:
    return bool(TOKEN and CHAT_ID)


def _post(method: str, body: dict) -> None:
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def telegram(text: str) -> bool:
    if not enabled():
        return False
    try:
        _post("sendMessage", {"chat_id": CHAT_ID, "text": text[:4000], "disable_web_page_preview": True})
        return True
    except Exception as e:          # noqa: BLE001 — an alert must never break the request
        log.warning("telegram alert failed: %s", e)
        return False


def order_submitted(order: dict, base_url: str = "", has_proof: bool = False) -> bool:
    lines = [
        "💳 Lumio: payment to check",
        f"Order {order['order_no']} · {order['plan']} × {order['months']} mo",
        f"Amount {order['currency']} {order['amount']:.2f}",
        f"Account {order.get('email') or '-'}",
        f"Reference {order.get('reference') or '-'}" + (" · screenshot attached in admin" if has_proof else ""),
    ]
    if base_url:
        lines.append(f"{base_url.rstrip('/')}/admin.html")
    return telegram("\n".join(lines))
