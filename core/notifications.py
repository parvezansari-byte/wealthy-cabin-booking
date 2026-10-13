"""E-mail notifications using a transactional outbox.

1. book_cabin / cancel_booking insert a notification_logs row in the SAME
   transaction as the booking change (unique per booking+kind, so never twice).
2. After commit, ``NotificationWorker.process`` claims due rows (row locks with
   SKIP LOCKED, so concurrent workers never claim the same row), sends them, and
   records 'sent' or 'failed' with exponential back-off (max 5 attempts).
3. A crashed worker's claim expires after 10 minutes and is retried.

Delivery is at-least-once; an e-mail failure can never undo or duplicate a booking.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from zoneinfo import ZoneInfo

from config import SmtpConfig
from core.security import esc
from core.timeutil import fmt_date, fmt_range, parse_ts

log = logging.getLogger(__name__)

SUBJECTS = {
    "booking_confirmed": "Booking confirmed",
    "booking_cancelled": "Booking cancelled",
    "booking_cancelled_by_admin": "Your booking was cancelled by an administrator",
    "maintenance_cancellation": "Your booking was cancelled for maintenance",
    "reminder": "Reminder: your meeting starts soon",
}

INTROS = {
    "booking_confirmed": "Your meeting cabin is booked.",
    "booking_cancelled": "Your booking has been cancelled as requested.",
    "booking_cancelled_by_admin": "An administrator cancelled the booking below.",
    "maintenance_cancellation": "The cabin was blocked for maintenance, so the booking below was cancelled.",
    "reminder": "This is a reminder about your upcoming meeting.",
}


def render_email(item: dict) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for a claimed notification row."""
    tz = ZoneInfo(item.get("timezone") or "Asia/Kolkata")
    start, end = parse_ts(item["start_at"]), parse_ts(item["end_at"])
    kind = item["kind"]
    app = item.get("app_name") or "Wealthy Office Cabin Booking"
    subject = f"{SUBJECTS[kind]} · {item['cabin_name']} · {fmt_date(start, tz)} · {item['reference']}"

    rows = [
        ("Booking reference", item["reference"]),
        ("Cabin", item["cabin_name"] + (f" ({item['cabin_location']})" if item.get("cabin_location") else "")),
        ("Date", fmt_date(start, tz)),
        ("Time", f"{fmt_range(start, end, tz)} ({tz.key})"),
        ("Meeting", item.get("title") or ""),
    ]
    if kind != "booking_confirmed" and kind != "reminder" and item.get("cancel_reason"):
        rows.append(("Reason", item["cancel_reason"]))

    name = item.get("employee_name") or ""
    greeting = f"Hello {name}," if name else "Hello,"
    text = "\n".join([greeting, "", INTROS[kind], ""] + [f"{k}: {v}" for k, v in rows]
                     + ["", f"— {app}", "This is an automated message; please do not reply."])

    html_rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#5b6475">{esc(k)}</td>'
        f'<td style="padding:6px 0;color:#0f172a;font-weight:600">{esc(v)}</td></tr>' for k, v in rows)
    html = f"""<!doctype html><html><body style="margin:0;background:#f4f6fb;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;border:1px solid #e3e8f2">
<tr><td style="padding:20px 28px;border-bottom:1px solid #e3e8f2;font-size:15px;font-weight:700;color:#1e3a8a">{esc(app)}</td></tr>
<tr><td style="padding:24px 28px;font-size:14px;color:#0f172a;line-height:1.5">
<p style="margin:0 0 8px">{esc(greeting)}</p><p style="margin:0 0 16px">{esc(INTROS[kind])}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="font-size:14px">{html_rows}</table>
</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #e3e8f2;font-size:12px;color:#5b6475">
This is an automated message; please do not reply.</td></tr></table></td></tr></table></body></html>"""
    return subject, text, html


class SmtpMailer:
    def __init__(self, cfg: SmtpConfig, app_name: str):
        self.cfg = cfg
        self.app_name = app_name

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        msg = EmailMessage()
        msg["From"] = formataddr((self.app_name, self.cfg.sender))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.cfg.sender.rsplit("@", 1)[-1])
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        if self.cfg.use_ssl:
            server = smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_seconds,
                                      context=context)
        else:
            server = smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_seconds)
        with server:
            if not self.cfg.use_ssl and self.cfg.starttls:
                server.starttls(context=context)
            if self.cfg.username:
                server.login(self.cfg.username, self.cfg.password)
            server.send_message(msg)


def _safe_error(exc: Exception) -> str:
    # Exception class and SMTP code only; never echo server banners or credentials.
    code = getattr(exc, "smtp_code", None)
    return f"{type(exc).__name__}{f' (SMTP {code})' if code else ''}"


class NotificationWorker:
    def __init__(self, service_client, mailer, enabled: bool = True):
        self.client = service_client
        self.mailer = mailer
        self.enabled = enabled

    def _complete(self, notification_id: str, outcome: str, error: str | None = None) -> None:
        self.client.rpc("complete_notification",
                        {"p_id": notification_id, "p_outcome": outcome, "p_error": error}).execute()

    def queue_reminders(self) -> int:
        return int(self.client.rpc("queue_due_reminders", {}).execute().data or 0)

    def process(self, limit: int = 20) -> dict:
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        items = self.client.rpc("claim_notifications", {"p_limit": limit}).execute().data or []
        for item in items:
            nid = item["id"]
            if not self.enabled:
                self._complete(nid, "skipped", "notifications disabled")
                counts["skipped"] += 1
                continue
            if item["kind"] in ("reminder", "booking_confirmed") and item.get("booking_status") != "confirmed":
                self._complete(nid, "skipped", "booking no longer confirmed")
                counts["skipped"] += 1
                continue
            try:
                subject, text, html = render_email(item)
                self.mailer.send(item["recipient_email"], subject, text, html)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
                log.warning("Notification %s failed: %s", nid, _safe_error(exc))
                self._complete(nid, "failed", _safe_error(exc))
                counts["failed"] += 1
                continue
            self._complete(nid, "sent")
            counts["sent"] += 1
        return counts


def build_worker(cfg):
    """Worker for the app server, or None when e-mail is not configured."""
    if not cfg.can_send_email:
        return None
    from core.database import create_service_client

    return NotificationWorker(create_service_client(cfg.supabase_url, cfg.supabase_service_role_key),
                              SmtpMailer(cfg.smtp, cfg.app_name), enabled=cfg.notifications_enabled)
