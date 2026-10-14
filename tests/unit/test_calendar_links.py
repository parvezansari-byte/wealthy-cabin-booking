from datetime import datetime, timezone
from email import message_from_bytes
from urllib.parse import parse_qs, urlparse

from config import SmtpConfig
from core.calendar_links import CalendarEvent, google_url, ics, ics_filename, outlook_url
from core.notifications import SmtpMailer, calendar_attachment

UTC = timezone.utc
BOOKING = {"id": "8f0c", "reference": "WCB-100001", "title": "Partner meet; Q4, plan",
           "start_at": "2026-10-14T08:15:00+00:00", "end_at": "2026-10-14T10:15:00+00:00", "attendees": 2}


def ev():
    return CalendarEvent.from_booking(BOOKING, "Cabin 01", "Floor 1 · East wing", "Wealthy Office Cabin Booking",
                                      "https://wealthy-cabins.streamlit.app")


def test_google_link_has_utc_times_and_details():
    q = parse_qs(urlparse(google_url(ev())).query)
    assert q["action"] == ["TEMPLATE"]
    assert q["dates"] == ["20261014T081500Z/20261014T101500Z"]  # 13:45–15:45 IST
    assert q["text"] == ["Partner meet; Q4, plan"]
    assert "Cabin 01, Floor 1 · East wing" in q["location"][0]
    assert "WCB-100001" in q["details"][0]


def test_outlook_links():
    work = urlparse(outlook_url(ev(), work=True))
    assert work.netloc == "outlook.office.com"
    q = parse_qs(work.query)
    assert q["startdt"] == ["2026-10-14T08:15:00Z"] and q["enddt"] == ["2026-10-14T10:15:00Z"]
    assert urlparse(outlook_url(ev(), work=False)).netloc == "outlook.live.com"


def test_ics_is_valid_and_escaped():
    text = ics(ev(), now=datetime(2026, 10, 14, tzinfo=UTC))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261014T081500Z" in text and "DTEND:20261014T101500Z" in text
    assert r"SUMMARY:Partner meet\; Q4\, plan" in text
    assert "UID:8f0c@wealthy-cabin-booking" in text and "BEGIN:VALARM" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    cancel = ics(ev(), cancel=True, sequence=1)
    assert "METHOD:CANCEL" in cancel and "STATUS:CANCELLED" in cancel and "VALARM" not in cancel
    assert ics_filename(BOOKING) == "WCB-100001.ics"


def test_email_attachment_for_confirmations_and_cancellations():
    item = {"kind": "booking_confirmed", "booking_id": "8f0c", "reference": "WCB-100001",
            "title": "Partner meet", "start_at": BOOKING["start_at"], "end_at": BOOKING["end_at"],
            "cabin_name": "Cabin 01", "cabin_location": None, "app_name": "Wealthy Office Cabin Booking"}
    name, body = calendar_attachment(item)
    assert name == "WCB-100001.ics" and "METHOD:PUBLISH" in body
    assert "METHOD:CANCEL" in calendar_attachment({**item, "kind": "booking_cancelled_by_admin"})[1]
    assert calendar_attachment({**item, "kind": "reminder"}) is None


def test_smtp_message_carries_text_calendar_part(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    mailer = SmtpMailer(SmtpConfig(host="smtp.example.com", sender="cabins@wealthy.in"), "Wealthy")
    mailer.send("a@wealthy.in", "Booked", "text", "<p>html</p>", calendar=("WCB-1.ics", ics(ev())))
    parsed = message_from_bytes(sent["msg"].as_bytes())
    parts = [p for p in parsed.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1 and parts[0].get_filename() == "WCB-1.ics"
    assert parts[0].get_param("method") == "PUBLISH"
