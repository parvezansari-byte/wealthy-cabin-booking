"""Admin controls (roles, access, maintenance) and the notification outbox."""

import pytest

from tests.db.conftest import DbError, slot


def svc(db, query, params=None):
    return db.call(None, query, params, role="service_role")


def claim(db, limit=10):
    return svc(db, "select cabins.claim_notifications(%s)", (limit,))[0][0]


# ------------------------------------------------------------------ admin controls
def test_admin_promotes_and_demotes_but_not_self(db):
    admin, uid = db.admin(), db.employee()
    db.call(admin, "select cabins.admin_set_role(%s, 'admin')", (uid,))
    assert db.call(uid, "select cabins.get_my_access()")[0][0]["is_admin"] is True
    db.call(admin, "select cabins.admin_set_role(%s, 'employee')", (uid,))
    assert db.call(uid, "select cabins.get_my_access()")[0][0]["is_admin"] is False
    with pytest.raises(DbError) as exc:
        db.call(admin, "select cabins.admin_set_role(%s, 'employee')", (admin,))
    assert exc.value.code == "CANNOT_CHANGE_OWN_ROLE"
    actions = [a for (a,) in db.owner("select action from cabins.audit_logs")]
    assert actions.count("profile.role_changed") >= 2


def test_pending_user_cannot_be_made_admin(db):
    admin = db.admin()
    pending = db.create_user("pending@wealthy.in")
    with pytest.raises(DbError) as exc:
        db.call(admin, "select cabins.admin_set_role(%s, 'admin')", (pending,))
    assert exc.value.code == "EMPLOYEE_NOT_ACTIVE"


def test_admin_approves_pending_employee(db):
    admin = db.admin()
    uid = db.create_user("approve.me@wealthy.in")
    db.call(admin, "select cabins.admin_set_employee_status(%s, 'active')", (uid,))
    assert db.status(uid) == "active"
    assert db.call(uid, "select cabins.get_my_access()")[0][0]["is_employee"] is True


def test_disabling_employee_keeps_history_and_can_cancel_future(db):
    admin, uid = db.admin(), db.employee()
    start, end = slot()
    booking = db.book(uid, db.cabin()["id"], start, end)
    res = db.call(admin, "select cabins.admin_set_employee_status(%s, 'disabled', true)", (uid,))[0][0]
    assert res["cancelled_bookings"] == 1
    assert db.owner("select status from cabins.bookings where id = %s", (booking["id"],))[0][0] == "cancelled"
    assert db.owner("select count(*) from cabins.profiles where id = %s", (uid,))[0][0] == 1
    with pytest.raises(DbError) as exc:
        db.book(uid, db.cabin("C02")["id"], start, end)
    assert exc.value.code == "EMPLOYEE_NOT_AUTHORIZED"
    db.call(admin, "select cabins.admin_set_employee_status(%s, 'active')", (uid,))
    assert db.book(uid, db.cabin("C02")["id"], start, end)["id"]


def test_admin_cannot_disable_self(db):
    admin = db.admin()
    with pytest.raises(DbError) as exc:
        db.call(admin, "select cabins.admin_set_employee_status(%s, 'disabled')", (admin,))
    assert exc.value.code == "CANNOT_CHANGE_OWN_ACCESS"


def test_maintenance_over_bookings_requires_confirmation_then_cancels_and_notifies(db):
    admin, uid, cabin = db.admin(), db.employee(), db.cabin("C06")["id"]
    start, end = slot(hh=13)
    booking = db.book(uid, cabin, start, end)
    with pytest.raises(DbError) as exc:
        db.call(admin, "select cabins.admin_create_maintenance(%s, %s, %s, 'Projector swap')",
                (cabin, start, end))
    assert exc.value.code == "MAINTENANCE_CONFLICTS_BOOKINGS"
    res = db.call(admin, "select cabins.admin_create_maintenance(%s, %s, %s, 'Projector swap', true)",
                  (cabin, start, end))[0][0]
    assert res["cancelled_bookings"] == 1
    row = db.owner("select status, cancel_reason from cabins.bookings where id = %s", (booking["id"],))[0]
    assert row == ("cancelled", "Maintenance: Projector swap")
    kinds = {k for (k,) in db.owner("select kind from cabins.notification_logs where booking_id = %s",
                                    (booking["id"],))}
    assert "maintenance_cancellation" in kinds
    db.call(admin, "select cabins.admin_remove_maintenance(%s)", (res["id"],))
    assert db.book(uid, cabin, start, end)["id"]


def test_employee_cannot_create_maintenance(db):
    uid = db.employee()
    start, end = slot()
    with pytest.raises(DbError) as exc:
        db.call(uid, "select cabins.admin_create_maintenance(%s, %s, %s, 'nope')", (db.cabin()["id"], start, end))
    assert exc.value.code == "ADMIN_REQUIRED"


def test_settings_changes_are_audited(db):
    admin = db.admin()
    db.call(admin, "update cabins.office_settings set open_time = '09:30' where id")
    row = db.owner("select actor_id, details from cabins.audit_logs where action = 'office_settings.update'")[0]
    assert str(row[0]) == admin and row[1]["open_time"]["new"] == "09:30:00"


# ------------------------------------------------------------------ notification outbox
def test_claim_send_and_complete(db):
    uid = db.employee()
    start, end = slot()
    booking = db.book(uid, db.cabin()["id"], start, end, title="Planning")
    items = claim(db)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "booking_confirmed" and item["reference"] == booking["reference"]
    assert item["recipient_email"].endswith("@wealthy.in") and item["timezone"] == "Asia/Kolkata"
    # A claimed row is not handed out twice.
    assert claim(db) == []
    svc(db, "select cabins.complete_notification(%s, 'sent')", (item["id"],))
    assert db.owner("select status from cabins.notification_logs where id = %s", (item["id"],))[0][0] == "sent"


def test_failed_notification_backs_off_and_admin_can_retry(db):
    uid, admin = db.employee(), db.admin()
    start, end = slot()
    db.book(uid, db.cabin()["id"], start, end)
    item = claim(db)[0]
    svc(db, "select cabins.complete_notification(%s, 'failed', 'SMTPConnectError')", (item["id"],))
    status, nxt = db.owner("select status, next_attempt_at > now() from cabins.notification_logs where id = %s",
                           (item["id"],))[0]
    assert status == "failed" and nxt is True
    assert claim(db) == []  # still backing off
    assert db.call(admin, "select cabins.admin_retry_failed_notifications()")[0][0] == 1
    assert len(claim(db)) == 1
    # The booking itself was never affected by the e-mail failure.
    assert db.owner("select count(*) from cabins.bookings where status = 'confirmed'")[0][0] == 1


def test_stale_claims_are_reclaimed(db):
    uid = db.employee()
    start, end = slot()
    db.book(uid, db.cabin()["id"], start, end)
    item = claim(db)[0]
    db.owner("update cabins.notification_logs set locked_at = now() - interval '11 minutes' where id = %s",
             (item["id"],))
    again = claim(db)
    assert len(again) == 1 and again[0]["attempts"] == 2


def test_notifications_are_unique_per_event(db):
    uid = db.employee()
    start, end = slot()
    booking = db.book(uid, db.cabin()["id"], start, end)
    db.owner("select cabins_private.queue_notification(%s, 'booking_confirmed')", (booking["id"],))
    assert db.owner("select count(*) from cabins.notification_logs")[0][0] == 1


def test_reminders_are_queued_once_for_soon_bookings(db):
    uid = db.employee()
    start, end = slot(days=2)
    booking = db.book(uid, db.cabin()["id"], start, end)
    assert svc(db, "select cabins.queue_due_reminders()")[0][0] == 0
    db.owner("""update cabins.bookings set start_at = now() + interval '10 minutes',
                end_at = now() + interval '40 minutes' where id = %s""", (booking["id"],))
    assert svc(db, "select cabins.queue_due_reminders()")[0][0] == 1
    assert svc(db, "select cabins.queue_due_reminders()")[0][0] == 0
