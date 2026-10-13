"""Booking rules enforced by cabins.book_cabin() and the table constraints."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.db.conftest import IST, DbError, slot


def code_of(db, uid, cabin, start, end, **kw):
    with pytest.raises(DbError) as exc:
        db.book(uid, cabin, start, end, **kw)
    return exc.value.code


def test_successful_booking_returns_reference_and_queues_email(db):
    uid, cabin = db.employee(), db.cabin("C03")
    start, end = slot(minutes=45)
    booking = db.book(uid, cabin["id"], start, end, title="Quarterly review")
    assert booking["reference"].startswith("WCB-")
    assert booking["cabin_name"] == "Cabin 03"
    row = db.owner("select user_id, status, start_at, end_at from cabins.bookings where id = %s",
                   (booking["id"],))[0]
    assert str(row[0]) == uid and row[1] == "confirmed"
    assert row[2] == start and row[3] == end  # stored as exact instants
    kinds = db.owner("select kind, status from cabins.notification_logs where booking_id = %s", (booking["id"],))
    assert kinds == [("booking_confirmed", "pending")]
    actions = db.owner("select action from cabins.audit_logs where entity_id = %s", (booking["id"],))
    assert ("booking.created",) in actions


def test_identity_comes_from_jwt_not_parameters(db):
    """There is no user parameter: the owner is always auth.uid()."""
    me = db.employee()
    start, end = slot()
    booking = db.book(me, db.cabin()["id"], start, end)
    owner = db.owner("select user_id from cabins.bookings where id = %s", (booking["id"],))[0][0]
    assert str(owner) == me


def test_exactly_120_minutes_is_allowed(db):
    start, end = slot(minutes=120)
    assert db.book(db.employee(), db.cabin()["id"], start, end)["id"]


def test_more_than_120_minutes_is_rejected(db):
    start, end = slot(minutes=121)
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "DURATION_TOO_LONG"


def test_120_minute_ceiling_holds_even_if_settings_are_tampered(db):
    with pytest.raises(Exception):
        db.settings(max_booking_minutes=180)  # check constraint
    uid, cabin = db.employee(), db.cabin()
    start, end = slot(minutes=150)
    with pytest.raises(Exception) as exc:  # table constraint, even for the owner
        db.owner("insert into cabins.bookings (cabin_id, user_id, start_at, end_at, title) "
                 "values (%s, %s, %s, %s, 'Too long')", (cabin["id"], uid, start, end))
    assert "bookings_max_duration" in str(exc.value)


def test_below_minimum_duration_is_rejected(db):
    start, end = slot(minutes=10)
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "DURATION_TOO_SHORT"


def test_configured_lower_maximum_is_enforced(db):
    db.settings(max_booking_minutes=60)
    start, end = slot(minutes=75)
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "DURATION_TOO_LONG"


def test_end_before_start_is_rejected(db):
    start, end = slot()
    assert code_of(db, db.employee(), db.cabin()["id"], end, start) == "INVALID_TIME_RANGE"


def test_overlapping_booking_is_rejected(db):
    a, b, cabin = db.employee(), db.employee(), db.cabin()["id"]
    start, end = slot(hh=10, minutes=60)
    db.book(a, cabin, start, end)
    assert code_of(db, b, cabin, start + timedelta(minutes=30), end + timedelta(minutes=30)) \
        == "CABIN_ALREADY_BOOKED"
    assert code_of(db, b, cabin, start - timedelta(minutes=15), end + timedelta(minutes=15)) \
        == "CABIN_ALREADY_BOOKED"
    assert code_of(db, b, cabin, start + timedelta(minutes=15), end - timedelta(minutes=15)) \
        == "CABIN_ALREADY_BOOKED"


def test_adjacent_bookings_do_not_overlap(db):
    a, b, cabin = db.employee(), db.employee(), db.cabin()["id"]
    start, end = slot(hh=10, minutes=60)
    db.book(a, cabin, start, end)
    db.book(b, cabin, end, end + timedelta(minutes=60))                       # starts as the first ends
    db.book(b, cabin, start - timedelta(minutes=60), start)                   # ends as the first starts
    assert db.owner("select count(*) from cabins.bookings where cabin_id = %s", (cabin,))[0][0] == 3


def test_same_time_different_cabins_is_allowed(db):
    uid = db.employee()
    start, end = slot()
    for code in ("C01", "C02", "C03"):
        db.book(uid, db.cabin(code)["id"], start, end)
    assert db.owner("select count(*) from cabins.bookings")[0][0] == 3


def test_outside_office_hours_is_rejected(db):
    uid, cabin = db.employee(), db.cabin()["id"]
    early_start, early_end = slot(hh=7, mm=30, minutes=60)        # starts before 08:00
    assert code_of(db, uid, cabin, early_start, early_end) == "OUTSIDE_OFFICE_HOURS"
    late_start, late_end = slot(hh=19, mm=30, minutes=60)         # ends after 20:00
    assert code_of(db, uid, cabin, late_start, late_end) == "OUTSIDE_OFFICE_HOURS"
    edge_start, edge_end = slot(hh=19, mm=0, minutes=60)          # ends exactly at close
    assert db.book(uid, cabin, edge_start, edge_end)["id"]


def test_non_working_day_is_rejected(db):
    start, end = slot(days=2)
    weekday = start.isoweekday()
    db.settings(working_days=[d for d in range(1, 8) if d != weekday])
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "NON_WORKING_DAY"


def test_booking_in_the_past_is_rejected(db):
    start, end = slot(days=-1)
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "BOOKING_IN_PAST"


def test_booking_beyond_horizon_is_rejected(db):
    db.settings(booking_horizon_days=7)
    start, end = slot(days=9)
    assert code_of(db, db.employee(), db.cabin()["id"], start, end) == "BOOKING_TOO_FAR_AHEAD"


def test_seconds_precision_is_rejected(db):
    start, end = slot()
    assert code_of(db, db.employee(), db.cabin()["id"], start + timedelta(seconds=5), end) == "INVALID_TIME_RANGE"


def test_maintenance_block_prevents_booking(db):
    admin, uid, cabin = db.admin(), db.employee(), db.cabin()["id"]
    start, end = slot(hh=12, minutes=60)
    db.call(admin, "select cabins.admin_create_maintenance(%s, %s, %s, 'AC repair')",
            (cabin, start - timedelta(hours=1), end))
    assert code_of(db, uid, cabin, start, end) == "CABIN_UNDER_MAINTENANCE"
    # Adjacent to the block is fine.
    assert db.book(uid, cabin, end, end + timedelta(minutes=30))["id"]


def test_inactive_cabin_cannot_be_booked(db):
    cabin = db.cabin("C05")
    db.owner("update cabins.cabins set is_active = false where id = %s", (cabin["id"],))
    start, end = slot()
    assert code_of(db, db.employee(), cabin["id"], start, end) == "CABIN_INACTIVE"


def test_attendees_over_capacity_is_rejected(db):
    cabin = db.cabin("C08")  # capacity 2
    start, end = slot()
    assert code_of(db, db.employee(), cabin["id"], start, end, attendees=3) == "CAPACITY_EXCEEDED"


@pytest.mark.parametrize("title", ["", "  ", "ab", "x" * 121])
def test_meeting_purpose_is_required(db, title):
    start, end = slot()
    assert code_of(db, db.employee(), db.cabin()["id"], start, end, title=title) == "INVALID_TITLE"


def test_cancelled_booking_frees_the_slot(db):
    a, b, cabin = db.employee(), db.employee(), db.cabin()["id"]
    start, end = slot()
    first = db.book(a, cabin, start, end)
    db.call(a, "select cabins.cancel_booking(%s)", (first["id"],))
    assert db.book(b, cabin, start, end)["id"]


def _next_dst_transition(tz: ZoneInfo, days: int = 365):
    day = datetime.now(tz).date() + timedelta(days=2)
    prev = datetime.combine(day, time(12)).replace(tzinfo=tz).utcoffset()
    for i in range(1, days):
        d = day + timedelta(days=i)
        off = datetime.combine(d, time(12)).replace(tzinfo=tz).utcoffset()
        if off != prev:
            return d - timedelta(days=1), d
        prev = off
    return None


def test_office_hours_follow_local_time_across_dst(db):
    """In a DST zone, 09:00 local maps to different UTC instants before and after
    the change; office-hour checks must use local wall-clock time."""
    tz = ZoneInfo("America/New_York")
    found = _next_dst_transition(tz)
    if not found:
        pytest.skip("no DST transition found")
    before, after = found
    db.settings(timezone="America/New_York", open_time="09:00", close_time="17:00", booking_horizon_days=365)
    uid, cabin = db.employee(), db.cabin()["id"]
    for day in (before, after):
        start = datetime.combine(day, time(9)).replace(tzinfo=tz)
        db.book(uid, cabin, start, start + timedelta(hours=1))
        too_early = datetime.combine(day, time(8, 30)).replace(tzinfo=tz)
        assert code_of(db, uid, cabin, too_early, too_early + timedelta(hours=1)) == "OUTSIDE_OFFICE_HOURS"
    utc_hours = {r[0] for r in db.owner(
        "select extract(hour from start_at at time zone 'UTC')::int from cabins.bookings")}
    assert len(utc_hours) == 2  # same local 09:00, different UTC hour


def test_ist_storage_is_utc_instant(db):
    uid = db.employee()
    start = datetime.combine((datetime.now(IST) + timedelta(days=1)).date(), time(9, 30)).replace(tzinfo=IST)
    booking = db.book(uid, db.cabin()["id"], start, start + timedelta(minutes=30))
    utc = db.owner("select to_char(start_at at time zone 'UTC', 'HH24:MI') from cabins.bookings where id = %s",
                   (booking["id"],))[0][0]
    assert utc == "04:00"  # 09:30 IST = 04:00 UTC
