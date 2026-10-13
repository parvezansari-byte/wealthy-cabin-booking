from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.booking_service import (BookingService, Interval, Rules, cabin_status, can_employee_cancel,
                                  free_windows, is_free, next_available, validate_booking)
from core.errors import AppError
from core.timeutil import day_bounds, local_datetime, round_up

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RULES = Rules(timezone="Asia/Kolkata", open_time=time(9), close_time=time(19), working_days=(1, 2, 3, 4, 5),
              min_minutes=15, max_minutes=120, slot_minutes=15, horizon_days=30)
CABIN = {"id": "c1", "code": "C01", "name": "Cabin 01", "capacity": 4, "is_active": True}
# Tuesday 13 Oct 2026, 10:00 IST
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=IST).astimezone(UTC)
TUE, WED, SAT = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 17)


def v(**kw):
    args = dict(cabin=CABIN, day=WED, start=time(10), duration=60, title="Weekly sync", attendees=2,
                notes=None, now=NOW)
    args.update(kw)
    return validate_booking(RULES, **args)


def test_valid_request_builds_utc_instants():
    errors, req = v()
    assert errors == []
    assert req.start_at == datetime(2026, 10, 14, 4, 30, tzinfo=UTC)
    assert req.end_at - req.start_at == timedelta(minutes=60)


def test_hard_cap_of_120_minutes_even_if_configured_higher():
    assert Rules(max_minutes=240).max_minutes == 120
    errors, _ = v(duration=135)
    assert any("at most 120" in e for e in errors)
    assert v(duration=120)[0] == []


@pytest.mark.parametrize("kw,needle", [
    (dict(duration=10), "at least 15"),
    (dict(start=time(8, 30)), "between 09:00 and 19:00"),
    (dict(start=time(18, 30), duration=60), "between 09:00 and 19:00"),
    (dict(day=SAT), "cannot be booked on this day"),
    (dict(day=TUE, start=time(9, 30)), "already passed"),
    (dict(day=TUE + timedelta(days=45)), "up to 30 days"),
    (dict(title="ab"), "3 to 120"),
    (dict(attendees=5), "seats 4"),
    (dict(cabin=None), "choose a cabin"),
    (dict(cabin={**CABIN, "is_active": False}), "unavailable"),
])
def test_validation_errors(kw, needle):
    errors, req = v(**kw)
    assert req is None and any(needle.lower() in e.lower() for e in errors), errors


def test_booking_can_end_exactly_at_closing():
    assert v(start=time(18), duration=60)[0] == []


def test_start_options_skip_past_and_respect_minimum():
    opts = RULES.start_options(TUE, NOW)
    assert opts[0] == time(10, 0) and opts[-1] == time(18, 45)
    assert RULES.start_options(SAT, NOW) == []


def test_duration_options():
    assert RULES.duration_options()[0] == 15 and RULES.duration_options()[-1] == 120
    assert Rules(min_minutes=20, max_minutes=50, slot_minutes=15).duration_options() == [20, 35, 50]


def iv(cabin, h1, m1, h2, m2, day=WED, **kw):
    return Interval(cabin_id=cabin, start=datetime.combine(day, time(h1, m1), IST).astimezone(UTC),
                    end=datetime.combine(day, time(h2, m2), IST).astimezone(UTC), kind=kw.pop("kind", "booking"), **kw)


def at(h, m, day=WED):
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def test_half_open_intervals():
    busy = [iv("c1", 10, 0, 11, 0)]
    assert is_free(busy, "c1", at(11, 0), at(12, 0))      # adjacent after
    assert is_free(busy, "c1", at(9, 0), at(10, 0))       # adjacent before
    assert not is_free(busy, "c1", at(10, 59), at(11, 30))
    assert is_free(busy, "c2", at(10, 0), at(11, 0))      # other cabin


def test_cabin_status_states_and_next_available():
    busy = [iv("c1", 10, 0, 11, 0), iv("c1", 11, 0, 11, 30, is_mine=True)]
    s = cabin_status(RULES, CABIN, busy, at(10, 15), at(10, 45))
    assert s.state == "occupied" and s.label == "Booked"
    assert s.next_free == at(11, 30)
    mine = cabin_status(RULES, CABIN, busy, at(11, 0), at(11, 15))
    assert mine.state == "mine" and mine.label == "Your booking"
    free = cabin_status(RULES, CABIN, busy, at(12, 0), at(13, 0))
    assert free.state == "available"
    maint = cabin_status(RULES, CABIN, [iv("c1", 9, 0, 19, 0, kind="maintenance")], at(12, 0), at(13, 0))
    assert maint.state == "maintenance" and maint.next_free == at(9, 0, date(2026, 10, 15))
    assert cabin_status(RULES, {**CABIN, "is_active": False}, [], at(12, 0), at(13, 0)).state == "inactive"


def test_next_available_skips_weekend_and_short_gaps():
    friday = date(2026, 10, 16)
    busy = [iv("c1", 9, 0, 18, 50, day=friday)]  # only 10 min left on Friday (< 15 min minimum)
    assert next_available(RULES, busy, "c1", at(9, 0, friday)) == at(9, 0, date(2026, 10, 19))


def test_free_windows():
    busy = [iv("c1", 10, 0, 11, 0), iv("c1", 11, 5, 12, 0)]
    gaps = free_windows(RULES, busy, "c1", WED)
    assert gaps == [(at(9, 0), at(10, 0)), (at(12, 0), at(19, 0))]  # 5-minute gap dropped


def test_dst_nonexistent_time_is_rejected():
    ny = ZoneInfo("America/New_York")
    assert local_datetime(date(2026, 3, 8), time(2, 30), ny) is None
    rules = Rules(timezone="America/New_York", open_time=time(0), close_time=time(23), working_days=tuple(range(1, 8)),
                  horizon_days=365)
    errors, _ = validate_booking(rules, cabin=CABIN, day=date(2026, 3, 8), start=time(2, 30), duration=30,
                                 title="Night shift", attendees=1, notes=None,
                                 now=datetime(2026, 3, 1, tzinfo=UTC))
    assert any("does not exist" in e for e in errors)


def test_dst_day_bounds_and_duration_are_real_minutes():
    ny = ZoneInfo("America/New_York")
    lo, hi = day_bounds(date(2026, 11, 1), ny)       # fall back: 25-hour day
    assert hi - lo == timedelta(hours=25)
    rules = Rules(timezone="America/New_York", open_time=time(0), close_time=time(23), working_days=tuple(range(1, 8)),
                  horizon_days=365)
    errors, req = validate_booking(rules, cabin=CABIN, day=date(2026, 11, 1), start=time(1, 0), duration=120,
                                   title="Overnight", attendees=1, notes=None, now=datetime(2026, 10, 1, tzinfo=UTC))
    assert errors == [] and req.end_at - req.start_at == timedelta(minutes=120)


def test_round_up():
    assert round_up(datetime(2026, 1, 1, 10, 7, tzinfo=UTC), 15) == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    assert round_up(datetime(2026, 1, 1, 10, 15, tzinfo=UTC), 15) == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    assert round_up(datetime(2026, 1, 1, 10, 15, 1, tzinfo=UTC), 15) == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)


def test_employee_cancel_window():
    booking = {"status": "confirmed", "start_at": at(12, 0, TUE).isoformat()}
    assert can_employee_cancel(RULES, booking, NOW)
    strict = Rules(cancel_cutoff_minutes=180)
    assert not can_employee_cancel(strict, booking, NOW)  # only 2 h before start
    assert not can_employee_cancel(RULES, {**booking, "status": "cancelled"}, NOW)


# ------------------------------------------------------------------ service behaviour
class FakeRepo:
    def __init__(self, fail_with=None):
        self.calls, self.fail_with = [], fail_with

    def book_cabin(self, *args):
        self.calls.append(args)
        if self.fail_with:
            raise self.fail_with
        return {"id": "b1", "reference": "WCB-100001", "cabin_name": "Cabin 01",
                "start_at": args[1].isoformat(), "end_at": args[2].isoformat(), "status": "confirmed"}

    def cancel_booking(self, booking_id, reason):
        self.calls.append((booking_id, reason))
        return {"id": booking_id, "status": "cancelled"}


class BrokenNotifier:
    def process(self, limit=20):
        raise ConnectionError("smtp down")


def test_invalid_request_never_reaches_database():
    repo = FakeRepo()
    with pytest.raises(AppError):
        BookingService(repo, RULES).create(cabin=CABIN, day=WED, start=time(10), duration=180, title="x",
                                           attendees=1, notes=None, now=NOW)
    assert repo.calls == []


def test_notification_failure_does_not_fail_a_saved_booking():
    svc = BookingService(FakeRepo(), RULES, BrokenNotifier())
    out = svc.create(cabin=CABIN, day=WED, start=time(10), duration=60, title="Weekly sync", attendees=2,
                     notes=None, now=NOW)
    assert out.booking["reference"] == "WCB-100001"
    assert "retried" in out.email_note


def test_database_failure_is_reported_and_nothing_claimed():
    repo = FakeRepo(fail_with=AppError("NETWORK", "We couldn't reach the booking database."))
    with pytest.raises(AppError) as exc:
        BookingService(repo, RULES).create(cabin=CABIN, day=WED, start=time(10), duration=60, title="Weekly sync",
                                           attendees=2, notes=None, now=NOW)
    assert exc.value.code == "NETWORK"


def test_conflict_from_database_is_surfaced():
    repo = FakeRepo(fail_with=AppError("CABIN_ALREADY_BOOKED"))
    with pytest.raises(AppError) as exc:
        BookingService(repo, RULES).create(cabin=CABIN, day=WED, start=time(10), duration=60, title="Weekly sync",
                                           attendees=2, notes=None, now=NOW)
    assert "already booked" in exc.value.message


def test_cancel_passes_clean_reason():
    repo = FakeRepo()
    BookingService(repo, RULES).cancel("b1", "  moved\x00 online ")
    assert repo.calls == [("b1", "moved online")]


def test_local_datetime_round_trip_ist():
    assert local_datetime(WED, time(9, 30), IST).astimezone(UTC) == datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
