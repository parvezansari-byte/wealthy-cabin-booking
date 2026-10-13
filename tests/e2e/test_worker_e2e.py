"""The e-mail worker against the real database (service_role), with a fake SMTP mailer."""
from core.notifications import NotificationWorker
from tests.db.conftest import database, db, slot  # noqa: F401
from tests.e2e.fake_supabase import FakeSupabase


class Mailer:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send(self, to, subject, text, html):
        if self.fail:
            raise ConnectionRefusedError("smtp down")
        self.sent.append((to, subject, text))


def test_worker_delivers_confirmation_and_cancellation(db):  # noqa: F811
    uid = db.employee("mail.me@wealthy.in", name="Mail Me")
    start, end = slot()
    booking = db.book(uid, db.cabin("C05")["id"], start, end, title="Roadmap review")
    mailer = Mailer()
    worker = NotificationWorker(FakeSupabase(db.conninfo, service_role=True), mailer)
    assert worker.process() == {"sent": 1, "failed": 0, "skipped": 0}
    to, subject, text = mailer.sent[0]
    assert to == "mail.me@wealthy.in" and booking["reference"] in subject
    assert "Cabin 05" in text and "Roadmap review" in text
    db.call(uid, "select public.cancel_booking(%s)", (booking["id"],))
    assert worker.process()["sent"] == 1
    assert "Booking cancelled" in mailer.sent[1][1]
    statuses = db.owner("select kind, status from public.notification_logs order by created_at")
    assert statuses == [("booking_confirmed", "sent"), ("booking_cancelled", "sent")]


def test_worker_failure_keeps_booking_and_schedules_retry(db):  # noqa: F811
    uid = db.employee()
    start, end = slot()
    db.book(uid, db.cabin()["id"], start, end)
    worker = NotificationWorker(FakeSupabase(db.conninfo, service_role=True), Mailer(fail=True))
    assert worker.process()["failed"] == 1
    row = db.owner("select status, attempts, last_error from public.notification_logs")[0]
    assert row == ("failed", 1, "ConnectionRefusedError")
    assert db.owner("select status from public.bookings")[0][0] == "confirmed"
    assert worker.process() == {"sent": 0, "failed": 0, "skipped": 0}  # backing off, not re-sent


def test_reminders_via_worker(db):  # noqa: F811
    uid = db.employee()
    start, end = slot(days=2)
    booking = db.book(uid, db.cabin()["id"], start, end)
    db.owner("update public.notification_logs set status = 'sent'")
    db.owner("""update public.bookings set start_at = date_trunc('minute', now()) + interval '10 minutes',
                end_at = date_trunc('minute', now()) + interval '40 minutes' where id = %s""", (booking["id"],))
    mailer = Mailer()
    worker = NotificationWorker(FakeSupabase(db.conninfo, service_role=True), mailer)
    assert worker.queue_reminders() == 1
    assert worker.process()["sent"] == 1 and "Reminder" in mailer.sent[0][1]
