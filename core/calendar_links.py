"""'Add to calendar' support: Google / Outlook links and RFC 5545 (.ics) invites.

Nothing here talks to Google or Microsoft: the links open the person's own
calendar with the event pre-filled, and the .ics file works in any calendar app.
A stable UID per booking means a later CANCEL invite removes the same event.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

UTC = timezone.utc


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str
    description: str
    organizer_email: str = ""

    @classmethod
    def from_booking(cls, booking: dict, cabin_name: str, cabin_location: str | None,
                     app_name: str, app_url: str = "", organizer_email: str = "") -> "CalendarEvent":
        from core.timeutil import parse_ts

        location = cabin_name + (f", {cabin_location}" if cabin_location else "")
        lines = [f"Cabin: {location}", f"Booking reference: {booking.get('reference', '')}"]
        if booking.get("attendees"):
            lines.append(f"Attendees: {booking['attendees']}")
        lines.append(f"Booked with {app_name}" + (f": {app_url}" if app_url else ""))
        return cls(uid=f"{booking['id']}@wealthy-cabin-booking", title=booking.get("title") or "Meeting",
                   start=parse_ts(booking["start_at"]), end=parse_ts(booking["end_at"]),
                   location=location, description="\n".join(lines), organizer_email=organizer_email)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def google_url(ev: CalendarEvent) -> str:
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": ev.title, "dates": f"{_utc(ev.start)}/{_utc(ev.end)}",
        "details": ev.description, "location": ev.location})


def outlook_url(ev: CalendarEvent, work: bool = True) -> str:
    """work=True → Microsoft 365 (outlook.office.com); False → Outlook.com."""
    host = "outlook.office.com" if work else "outlook.live.com"
    iso = "%Y-%m-%dT%H:%M:%SZ"
    return f"https://{host}/calendar/0/deeplink/compose?" + urlencode({
        "path": "/calendar/action/compose", "rru": "addevent", "subject": ev.title,
        "startdt": ev.start.astimezone(UTC).strftime(iso), "enddt": ev.end.astimezone(UTC).strftime(iso),
        "body": ev.description, "location": ev.location})


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545 §3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def ics(ev: CalendarEvent, cancel: bool = False, sequence: int = 0, now: datetime | None = None) -> str:
    """A single-event calendar. cancel=True produces a METHOD:CANCEL update for the same UID."""
    stamp = _utc(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Wealthy//Office Cabin Booking//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancel else 'PUBLISH'}",
        "BEGIN:VEVENT",
        f"UID:{ev.uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(ev.start)}",
        f"DTEND:{_utc(ev.end)}",
        f"SUMMARY:{_escape(ev.title)}",
        f"LOCATION:{_escape(ev.location)}",
        f"DESCRIPTION:{_escape(ev.description)}",
        f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
    ]
    if ev.organizer_email:
        lines.append(f"ORGANIZER:mailto:{ev.organizer_email}")
    if not cancel:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(ev.title)}",
                  "TRIGGER:-PT15M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def ics_filename(booking: dict) -> str:
    ref = "".join(ch for ch in str(booking.get("reference") or "booking") if ch.isalnum() or ch == "-")
    return f"{ref}.ics"
