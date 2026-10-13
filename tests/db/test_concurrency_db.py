"""Simultaneous booking attempts: exactly one wins, the rest get a clean error."""
import threading
from datetime import timedelta

from tests.db.conftest import DbError, slot


def _race(db, attempts):
    """Start all attempts at the same instant on separate connections."""
    barrier = threading.Barrier(len(attempts))
    results = [None] * len(attempts)

    def run(i, uid, cabin, start, end):
        barrier.wait()
        try:
            results[i] = ("ok", db.book(uid, cabin, start, end))
        except DbError as exc:
            results[i] = ("error", exc.code)
        except Exception as exc:  # noqa: BLE001
            results[i] = ("crash", repr(exc))

    threads = [threading.Thread(target=run, args=(i, *a)) for i, a in enumerate(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_two_simultaneous_identical_requests(db):
    cabin = db.cabin()["id"]
    start, end = slot(hh=11)
    results = _race(db, [(db.employee(), cabin, start, end), (db.employee(), cabin, start, end)])
    assert sorted(r[0] for r in results) == ["error", "ok"]
    assert [r[1] for r in results if r[0] == "error"] == ["CABIN_ALREADY_BOOKED"]
    assert db.owner("select count(*) from public.bookings where status = 'confirmed'")[0][0] == 1


def test_many_simultaneous_overlapping_requests(db):
    cabin = db.cabin("C02")["id"]
    start, end = slot(hh=14)
    attempts = []
    for i in range(12):
        offset = timedelta(minutes=15 * (i % 4))
        attempts.append((db.employee(), cabin, start + offset, end + offset))
    results = _race(db, attempts)
    assert not [r for r in results if r[0] == "crash"], results
    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1
    assert {r[1] for r in results if r[0] == "error"} == {"CABIN_ALREADY_BOOKED"}
    # And the database agrees: no two confirmed bookings overlap.
    overlaps = db.owner("""
        select count(*) from public.bookings a join public.bookings b
          on a.cabin_id = b.cabin_id and a.id < b.id
         and tstzrange(a.start_at, a.end_at, '[)') && tstzrange(b.start_at, b.end_at, '[)')
         where a.status = 'confirmed' and b.status = 'confirmed'""")[0][0]
    assert overlaps == 0


def test_simultaneous_requests_for_different_cabins_all_succeed(db):
    start, end = slot(hh=15)
    attempts = [(db.employee(), db.cabin(f"C{n:02d}")["id"], start, end) for n in range(1, 11)]
    results = _race(db, attempts)
    assert [r[0] for r in results] == ["ok"] * 10


def test_maintenance_and_booking_race_never_both_win_silently(db):
    """Creating maintenance (without cancelling) and booking at the same moment:
    either the booking is refused, or maintenance creation reports the conflict."""
    admin, uid, cabin = db.admin(), db.employee(), db.cabin("C04")["id"]
    start, end = slot(hh=16)
    barrier = threading.Barrier(2)
    out = {}

    def book():
        barrier.wait()
        try:
            out["book"] = ("ok", db.book(uid, cabin, start, end))
        except DbError as exc:
            out["book"] = ("error", exc.code)

    def block():
        barrier.wait()
        try:
            out["block"] = ("ok", db.call(admin, "select public.admin_create_maintenance(%s, %s, %s, 'Deep clean')",
                                          (cabin, start, end)))
        except DbError as exc:
            out["block"] = ("error", exc.code)

    threads = [threading.Thread(target=book), threading.Thread(target=block)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert {out["book"][0], out["block"][0]} == {"ok", "error"}
    assert out["book"][1] == "CABIN_UNDER_MAINTENANCE" or out["block"][1] == "MAINTENANCE_CONFLICTS_BOOKINGS"
