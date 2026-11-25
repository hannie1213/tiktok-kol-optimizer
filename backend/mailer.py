"""
Transactional email — SMTP via the standard library (no extra dependency).

Works with any SMTP relay (Resend, Brevo, Mailgun, Amazon SES, Gmail/Outlook
app passwords, Tencent/Aliyun enterprise mail …).

Configuration (env vars)
------------------------
  SMTP_HOST        Relay host. Unset → email is disabled: nothing is sent and
                   the product falls back to operator-assisted flows.
  SMTP_PORT        Default 587 (STARTTLS); 465 → implicit TLS.
  SMTP_USER        Login (often "apikey"/"resend" or the mailbox address).
  SMTP_PASSWORD    Password or API key.
  SMTP_FROM        From header, e.g. "Lumio <no-reply@yourdomain.com>".
                   Defaults to SMTP_USER.
  SMTP_SECURITY    starttls (default) | ssl | none. Port 465 implies ssl.
  APP_BASE_URL     Public origin used to build links in emails, e.g.
                   https://app.yourdomain.com. Required in production when
                   email is on — links are never built from the request's
                   Host header there, since a forged Host would let an
                   attacker point a victim's reset link at their own site.

Messages are sent off the request path (FastAPI BackgroundTasks) and a
delivery failure is logged, never raised: email must not break sign-up.
"""
import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional

log = logging.getLogger("kol.mail")

SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)
SMTP_USER = (os.environ.get("SMTP_USER") or "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or ""
SMTP_FROM = (os.environ.get("SMTP_FROM") or SMTP_USER).strip()
SMTP_SECURITY = (os.environ.get("SMTP_SECURITY") or ("ssl" if SMTP_PORT == 465 else "starttls")).strip().lower()
APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")

BRAND = "Lumio"


def enabled() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def base_url(request=None) -> str:
    """Origin for links in emails: APP_BASE_URL, else (dev only) the request's."""
    if APP_BASE_URL:
        return APP_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


# ── Delivery ──────────────────────────────────────────────────────
def _deliver(msg: EmailMessage):
    """Hand one message to the relay. Tests replace this function."""
    ctx = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
    with server:
        if SMTP_SECURITY == "starttls":
            server.starttls(context=ctx)
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def send(to: str, subject: str, text: str, html_body: Optional[str] = None) -> bool:
    """Send one email. Returns False (and logs) instead of raising."""
    if not enabled():
        return False
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=SMTP_FROM.split("@")[-1].strip("> ") or None)
    msg["Auto-Submitted"] = "auto-generated"      # keeps auto-responders quiet
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        _deliver(msg)
        log.info("mail sent subject=%r", subject)
        return True
    except Exception:
        # The recipient is deliberately not logged (personal data).
        log.exception("mail delivery failed subject=%r", subject)
        return False


# ── Templates (zh / en) ───────────────────────────────────────────
def _layout(heading: str, paragraphs: list, button: Optional[tuple], footer: str) -> str:
    """Minimal, client-safe HTML (tables/inline styles only)."""
    body = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:1.65;color:#374151">{html.escape(p)}</p>'
        for p in paragraphs
    )
    cta = ""
    if button:
        label, url = button
        safe_url = html.escape(url, quote=True)
        cta = (
            f'<p style="margin:22px 0"><a href="{safe_url}" style="display:inline-block;background:#111827;'
            f'color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;padding:12px 22px;'
            f'border-radius:10px">{html.escape(label)}</a></p>'
            f'<p style="margin:0 0 14px;font-size:12px;line-height:1.6;color:#6b7280;word-break:break-all">'
            f'{safe_url}</p>'
        )
    return (
        '<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;padding:24px 12px;'
        'font-family:-apple-system,Segoe UI,Roboto,PingFang SC,Microsoft YaHei,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;'
        'background:#ffffff;border-radius:14px;padding:28px 26px"><tr><td>'
        f'<p style="margin:0 0 18px;font-size:13px;font-weight:800;letter-spacing:.04em;color:#0FB5AE">{BRAND}</p>'
        f'<h1 style="margin:0 0 16px;font-size:20px;line-height:1.35;color:#111827">{html.escape(heading)}</h1>'
        f'{body}{cta}'
        f'<p style="margin:22px 0 0;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;'
        f'line-height:1.6;color:#9ca3af">{html.escape(footer)}</p>'
        '</td></tr></table></td></tr></table></body></html>'
    )


def _compose(heading: str, paragraphs: list, footer: str, button: Optional[tuple] = None) -> tuple:
    lines = [heading, ""] + paragraphs
    if button:
        lines += ["", f"{button[0]}: {button[1]}"]
    lines += ["", "—", footer]
    return "\n".join(lines), _layout(heading, paragraphs, button, footer)


def _zh(lang: str) -> bool:
    return (lang or "").lower().startswith("zh")


def send_verification(to: str, link: str, lang: str = "en") -> bool:
    if _zh(lang):
        subject = f"请验证你的 {BRAND} 邮箱"
        text, body = _compose(
            "欢迎使用 Lumio，请验证你的邮箱",
            ["你的账号已创建。点击下方按钮确认这个邮箱属于你——之后找回密码、接收套餐与付款通知都会发到这里。",
             "链接 48 小时内有效。"],
            "如果这不是你本人注册的，忽略这封邮件即可。",
            ("验证邮箱", link))
    else:
        subject = f"Verify your {BRAND} email"
        text, body = _compose(
            "Welcome to Lumio — please verify your email",
            ["Your account is ready. Confirm that this address is yours — password resets and plan "
             "and payment notices are sent here.",
             "The link is valid for 48 hours."],
            "If you didn't create this account, you can ignore this email.",
            ("Verify email", link))
    return send(to, subject, text, body)


def send_password_reset(to: str, link: str, lang: str = "en") -> bool:
    if _zh(lang):
        subject = f"重置你的 {BRAND} 密码"
        text, body = _compose(
            "重置密码",
            ["我们收到了重置你账号密码的请求。点击下方按钮设置新密码。",
             "链接 30 分钟内有效，且只能使用一次。"],
            "如果不是你本人操作，忽略这封邮件即可，你的密码不会改变。",
            ("设置新密码", link))
    else:
        subject = f"Reset your {BRAND} password"
        text, body = _compose(
            "Reset your password",
            ["We received a request to reset the password for your account. Use the button below to "
             "choose a new one.",
             "The link is valid for 30 minutes and works once."],
            "If you didn't ask for this, ignore this email — your password stays the same.",
            ("Choose a new password", link))
    return send(to, subject, text, body)


def send_password_changed(to: str, lang: str = "en") -> bool:
    if _zh(lang):
        subject = f"你的 {BRAND} 密码已修改"
        text, body = _compose(
            "密码已修改",
            ["你的账号密码刚刚被修改，其他设备上的登录已全部退出。"],
            "如果不是你本人操作，请立即用「忘记密码」重置，并联系我们。")
    else:
        subject = f"Your {BRAND} password was changed"
        text, body = _compose(
            "Your password was changed",
            ["The password for your account was just changed, and every other device was signed out."],
            "If this wasn't you, reset your password right away with \"Forgot your password?\" and contact us.")
    return send(to, subject, text, body)


def send_plan_changed(to: str, plan_name: str, link: str = "", lang: str = "en",
                      expires_on: str = "") -> bool:
    if _zh(lang):
        subject = f"你的 {BRAND} 套餐已变更为 {plan_name}"
        paragraphs = [f"你的账号现在是 {plan_name} 套餐，新的额度已立即生效。"]
        if expires_on:
            paragraphs.append(f"有效期至 {expires_on}。")
        text, body = _compose("套餐已更新", paragraphs, "感谢使用 Lumio。",
                              ("打开控制台", link) if link else None)
    else:
        subject = f"Your {BRAND} plan is now {plan_name}"
        paragraphs = [f"Your account is now on the {plan_name} plan. The new limits apply immediately."]
        if expires_on:
            paragraphs.append(f"It is active until {expires_on}.")
        text, body = _compose("Your plan was updated", paragraphs, "Thanks for using Lumio.",
                              ("Open the console", link) if link else None)
    return send(to, subject, text, body)


def send_plan_expiring(to: str, plan_name: str, days_left: int, expires_on: str,
                       link: str = "", lang: str = "en") -> bool:
    if _zh(lang):
        when = "明天" if days_left <= 1 else f"{days_left} 天后"
        subject = f"你的 {BRAND} {plan_name} 套餐将在{when}到期"
        text, body = _compose(
            f"{plan_name} 套餐即将到期",
            [f"你的 {plan_name} 套餐有效期至 {expires_on}。",
             "到期后账号会自动回到免费版：数据全部保留，但每月额度会降低，团队成员将无法登录。",
             "在到期前续费，新的期限会接在当前到期日之后，不会损失剩余天数。"],
            "如果你已经续费，请忽略这封邮件。",
            ("去续费", link) if link else None)
    else:
        when = "tomorrow" if days_left <= 1 else f"in {days_left} days"
        subject = f"Your {BRAND} {plan_name} plan ends {when}"
        text, body = _compose(
            f"Your {plan_name} plan is about to end",
            [f"Your {plan_name} plan is active until {expires_on}.",
             "After that the account returns to the Free plan: all your data stays, but the "
             "monthly allowances drop and team members can no longer sign in.",
             "Renew before then and the new term starts where this one ends — no days are lost."],
            "If you have already renewed, you can ignore this email.",
            ("Renew now", link) if link else None)
    return send(to, subject, text, body)


def send_plan_expired(to: str, plan_name: str, link: str = "", lang: str = "en") -> bool:
    if _zh(lang):
        subject = f"你的 {BRAND} {plan_name} 套餐已到期"
        text, body = _compose(
            f"{plan_name} 套餐已到期",
            ["你的账号已回到免费版。创作者库、活动和校准数据都还在。",
             "随时续费即可恢复原来的额度和团队成员的访问。"],
            "感谢使用 Lumio。", ("续费", link) if link else None)
    else:
        subject = f"Your {BRAND} {plan_name} plan has ended"
        text, body = _compose(
            f"Your {plan_name} plan has ended",
            ["Your account is back on the Free plan. Your creator library, campaigns and "
             "calibration are all still there.",
             "Renew any time to restore your allowances and your team's access."],
            "Thanks for using Lumio.", ("Renew", link) if link else None)
    return send(to, subject, text, body)


def send_team_invite(to: str, inviter: str, link: str, lang: str = "en") -> bool:
    if _zh(lang):
        subject = f"{inviter} 邀请你加入 {BRAND} 工作区"
        text, body = _compose(
            "你收到了一个团队邀请",
            [f"{inviter} 邀请你加入他们的 {BRAND} 工作区，一起管理创作者库和投放方案。",
             "点击下方按钮设置密码即可加入。链接 7 天内有效。"],
            "如果你不认识邀请人，请忽略这封邮件。", ("接受邀请", link))
    else:
        subject = f"{inviter} invited you to their {BRAND} workspace"
        text, body = _compose(
            "You've been invited to a team",
            [f"{inviter} invited you to join their {BRAND} workspace and work on the same "
             "creator library and campaign plans.",
             "Choose a password with the button below to join. The link works for 7 days."],
            "If you don't know the sender, you can ignore this email.", ("Accept invitation", link))
    return send(to, subject, text, body)


def send_order_to_operator(to: str, order: dict, base: str = "") -> bool:
    """Tell the operator a merchant says they have paid (operator-facing, Chinese)."""
    amount = f"{order['currency']} {order['amount']:.2f}"
    subject = f"[{BRAND}] 待确认付款 {amount} · {order['order_no']}"
    text, body = _compose(
        f"有一笔付款待确认：{amount}",
        [f"订单号：{order['order_no']}",
         f"账号：{order['email']}",
         f"套餐：{order['plan']} × {order['months']} 个月",
         f"应付金额：{amount}（金额唯一，可直接在钱包交易记录里按金额查找）",
         f"用户填写的交易参考号：{order.get('reference') or '—'}",
         "请在钱包 App 的收款记录中核对到账后，到运营后台点击「确认到账」。"],
        "这是系统自动通知。",
        ("打开运营后台", base + "/admin.html") if base else None)
    return send(to, subject, text, body)


def send_order_rejected(to: str, order: dict, base: str = "", lang: str = "en") -> bool:
    amount = f"{order['currency']} {order['amount']:.2f}"
    note = order.get("note") or ""
    if _zh(lang):
        subject = f"我们没有找到你的 {BRAND} 付款（订单 {order['order_no']}）"
        paragraphs = [f"我们没能在收款记录中找到订单 {order['order_no']}（{amount}）对应的付款。"]
        if note:
            paragraphs.append(f"备注：{note}")
        paragraphs.append("请核对付款金额与交易参考号后，在「套餐与账单」中重新提交；如已付款，直接回复联系我们即可。")
        text, body = _compose("付款未能确认", paragraphs, "如有疑问请联系我们。",
                              ("打开控制台", base) if base else None)
    else:
        subject = f"We couldn't find your {BRAND} payment (order {order['order_no']})"
        paragraphs = [f"We couldn't match order {order['order_no']} ({amount}) to a payment we received."]
        if note:
            paragraphs.append(f"Note: {note}")
        paragraphs.append("Please check the amount and transaction reference and resubmit under "
                          "Plan & Billing. If you did pay, just contact us.")
        text, body = _compose("Payment not confirmed", paragraphs, "Contact us if you need help.",
                              ("Open the console", base) if base else None)
    return send(to, subject, text, body)
