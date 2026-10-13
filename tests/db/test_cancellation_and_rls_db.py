"""Cancellation rules and Row Level Security."""
from datetime import timedelta

import pytest

from tests.db.conftest import DbError, slot


# ------------------------------------------------------------------ cancellation
def test_employee_cancels_own_booking_and_history_is_kept(db):
    uid = db.employee()
    start, end = slot()
    booking = db.book(uid, db.cabin()["id"], start, end)
    result = db.call(uid, "select public.cancel_booking(%s, 'Plans changed')", (booking["id"],))[0][0]
    assert result["status"] == "cancelled"
    row = db.owner("select status, cancelled_by, cancel_reason from public.bookings where id = %s",
                   (booking["id"],))[0]
    assert row[0] == "cancelled" and str(row[1]) == uid and row[2] == "Plans changed"
    kinds = {k for (k,) in db.owner("select kind from public.notification_logs where booking_id = %s",
                                    (booking["id"],))}
    assert kinds == {"booking_confirmed", "booking_cancelled"}
    assert db.owner("select count(*) from public.audit_logs where action = 'booking.cancelled'")[0][0] == 1


def test_employee_cannot_cancel_someone_elses_booking(db):
    owner, intruder = db.employee(), db.employee()
    start, end = slot()
    booking = db.book(owner, db.cabin()["id"], start, end)
    with pytest.raises(DbError) as exc:
        db.call(intruder, "select public.cancel_booking(%s)", (booking["id"],))
    assert exc.value.code == "BOOKING_NOT_FOUND"  # no hint that it exists
    with pytest.raises(DbError):
        db.call(intruder, "update public.bookings set status = 'cancelled' where id = %s", (booking["id"],))
    assert db.owner("select status from public.bookings where id = %s", (booking["id"],))[0][0] == "confirmed"


def test_admin_can_cancel_any_booking(db):
    owner, admin = db.employee(), db.admin()
    start, end = slot()
    booking = db.book(owner, db.cabin()["id"], start, end)
    db.call(admin, "select public.cancel_booking(%s, 'Board meeting')", (booking["id"],))
    kinds = {k for (k,) in db.owner("select kind from public.notification_logs where booking_id = %s",
                                    (booking["id"],))}
    assert "booking_cancelled_by_admin" in kinds


def test_cancellation_cutoff_applies_to_employees_not_admins(db):
    db.settings(cancel_cutoff_minutes=24 * 60)  # must cancel a day ahead
    owner, admin = db.employee(), db.admin()
    start, end = slot(days=3, hh=8, minutes=30)
    booking = db.book(owner, db.cabin()["id"], start, end)
    # Move it to start in 2 hours (inside the 24h cut-off) as the owner could not:
    near = db.owner("""update public.bookings set start_at = now() + interval '2 hours',
                       end_at = now() + interval '2 hours 30 minutes' where id = %s returning id""",
                    (booking["id"],))
    assert near
    with pytest.raises(DbError) as exc:
        db.call(owner, "select public.cancel_booking(%s)", (booking["id"],))
    assert exc.value.code == "CANCELLATION_WINDOW_CLOSED"
    db.call(admin, "select public.cancel_booking(%s)", (booking["id"],))


def test_cannot_cancel_twice_or_after_end(db):
    uid = db.employee()
    start, end = slot()
    booking = db.book(uid, db.cabin()["id"], start, end)
    db.call(uid, "select public.cancel_booking(%s)", (booking["id"],))
    with pytest.raises(DbError) as exc:
        db.call(uid, "select public.cancel_booking(%s)", (booking["id"],))
    assert exc.value.code == "ALREADY_CANCELLED"

    other = db.book(uid, db.cabin("C02")["id"], start, end)
    db.owner("""update public.bookings set start_at = now() - interval '2 hours',
                end_at = now() - interval '1 hour' where id = %s""", (other["id"],))
    with pytest.raises(DbError) as exc:
        db.call(db.admin(), "select public.cancel_booking(%s)", (other["id"],))
    assert exc.value.code == "BOOKING_ALREADY_ENDED"


# ------------------------------------------------------------------ RLS
def test_employees_only_see_their_own_bookings(db):
    a, b = db.employee(), db.employee()
    start, end = slot()
    db.book(a, db.cabin("C01")["id"], start, end, title="A private meeting", notes="Secret agenda")
    db.book(b, db.cabin("C02")["id"], start, end, title="B private meeting")
    rows = db.call(a, "select title from public.bookings")
    assert rows == [("A private meeting",)]


def test_availability_hides_other_peoples_meeting_details(db):
    a, b = db.employee(), db.employee()
    start, end = slot()
    db.book(a, db.cabin("C01")["id"], start, end, title="Salary review")
    rows = db.call(b, "select kind, is_mine, booking_id, title, booker_name from public.get_availability(%s, %s)",
                   (start - timedelta(hours=1), end + timedelta(hours=1)))
    assert rows == [("booking", False, None, None, None)]
    mine = db.call(a, "select is_mine, title from public.get_availability(%s, %s)", (start, end))
    assert mine == [(True, "Salary review")]


def test_booker_names_visible_only_when_enabled(db):
    a, b = db.employee(name="Asha Rao"), db.employee()
    start, end = slot()
    db.book(a, db.cabin()["id"], start, end)
    db.settings(show_booker_names=True)
    rows = db.call(b, "select booker_name, title from public.get_availability(%s, %s)", (start, end))
    assert rows == [("Asha Rao", None)]  # name shown, meeting title still private


def test_admin_sees_all_bookings_and_details(db):
    a, admin = db.employee(), db.admin()
    start, end = slot()
    db.book(a, db.cabin()["id"], start, end, title="Visible to admin")
    assert db.call(admin, "select title from public.bookings") == [("Visible to admin",)]


def test_direct_writes_to_bookings_are_denied(db):
    uid = db.employee()
    start, end = slot()
    with pytest.raises(DbError) as exc:
        db.call(uid, "insert into public.bookings (cabin_id, user_id, start_at, end_at, title) "
                     "values (%s, %s, %s, %s, 'sneaky')", (db.cabin()["id"], uid, start, end))
    assert exc.value.sqlstate == "42501"
    booking = db.book(uid, db.cabin()["id"], start, end)
    with pytest.raises(DbError):
        db.call(uid, "update public.bookings set end_at = end_at + interval '3 hours' where id = %s",
                (booking["id"],))
    with pytest.raises(DbError):
        db.call(uid, "delete from public.bookings where id = %s", (booking["id"],))


def test_pending_and_disabled_users_see_nothing(db):
    owner, admin = db.employee(), db.admin()
    start, end = slot()
    db.book(owner, db.cabin()["id"], start, end)
    db.call(admin, "select public.admin_set_employee_status(%s, 'disabled')", (owner,))
    assert db.call(owner, "select count(*) from public.bookings")[0][0] == 0
    assert db.call(owner, "select count(*) from public.cabins")[0][0] == 0
    with pytest.raises(DbError) as exc:
        db.call(owner, "select * from public.get_availability(%s, %s)", (start, end))
    assert exc.value.code == "EMPLOYEE_NOT_AUTHORIZED"
    # History is preserved, not deleted.
    assert db.owner("select count(*) from public.bookings where user_id = %s", (owner,))[0][0] == 1


def test_audit_and_notification_logs_are_admin_only(db):
    uid, admin = db.employee(), db.admin()
    start, end = slot()
    db.book(uid, db.cabin()["id"], start, end, notes="private notes")
    assert db.call(uid, "select count(*) from public.audit_logs")[0][0] == 0
    assert db.call(uid, "select count(*) from public.notification_logs")[0][0] == 0
    assert db.call(admin, "select count(*) from public.audit_logs")[0][0] > 0
    assert db.call(admin, "select count(*) from public.notification_logs")[0][0] == 1


def test_audit_log_never_contains_meeting_notes_and_is_immutable(db):
    uid = db.employee()
    start, end = slot()
    db.book(uid, db.cabin()["id"], start, end, notes="Confidential acquisition")
    dump = db.owner("select details::text from public.audit_logs")
    assert all("Confidential" not in d[0] for d in dump)
    with pytest.raises(Exception) as exc:
        db.owner("delete from public.audit_logs")
    assert "AUDIT_LOG_IMMUTABLE" in str(exc.value)


def test_employee_cannot_edit_cabins_or_settings(db):
    uid, admin = db.employee(), db.admin()
    cabin = db.cabin()["id"]
    assert db.call(uid, "update public.cabins set capacity = 50 where id = %s", (cabin,)) == 0
    assert db.call(uid, "update public.office_settings set close_time = '23:00' where id") == 0
    assert db.call(admin, "update public.cabins set capacity = 5 where id = %s", (cabin,)) == 1
    assert db.call(admin, "update public.office_settings set close_time = '18:00' where id") == 1


def test_admin_cannot_change_allowed_domains_through_api(db):
    admin = db.admin()
    with pytest.raises(DbError) as exc:
        db.call(admin, "update public.office_settings set allowed_email_domains = '{gmail.com}' where id")
    assert exc.value.sqlstate == "42501"


def test_employee_cannot_call_admin_functions(db):
    uid, other = db.employee(), db.employee()
    for query, params in [
        ("select public.admin_set_role(%s, 'admin')", (other,)),
        ("select public.admin_set_employee_status(%s, 'disabled')", (other,)),
        ("select public.admin_retry_failed_notifications()", None),
    ]:
        with pytest.raises(DbError) as exc:
            db.call(uid, query, params)
        assert exc.value.code == "ADMIN_REQUIRED"


def test_worker_functions_are_service_role_only(db):
    uid = db.admin()
    for fn in ("public.claim_notifications(5)", "public.queue_due_reminders()"):
        with pytest.raises(DbError) as exc:
            db.call(uid, f"select {fn}")
        assert exc.value.sqlstate == "42501"
