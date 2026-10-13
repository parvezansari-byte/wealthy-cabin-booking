"""Booking rules, availability computation and the booking/cancellation use cases.

The validation here mirrors public.book_cabin() so users get instant feedback,
but it is NOT the security boundary: the database function re-checks every
rule inside the same transaction that inserts the row, and the exclusion
constraint makes double-booking impossible even under concurrency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

from core.errors import AppError
from core.security import clean_text
from core.timeutil import UTC, local_datetime, minutes_between, now_utc, parse_ts, round_up

log = logging.getLogger(__name__)

HARD_MAX_MINUTES = 120


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value)[:8])


@dataclass(frozen=True)
class Rules:
    timezone: str = "Asia/Kolkata"
    open_time: time = time(9, 0)
    close_time: time = time(19, 0)
    working_days: tuple[int, ...] = (1, 2, 3, 4, 5)  # ISO weekday numbers
    min_minutes: int = 15
    max_minutes: int = 120
    slot_minutes: int = 15
    horizon_days: int = 30
    cancel_cutoff_minutes: int = 0

    def __post_init__(self):
        object.__setattr__(self, "max_minutes", min(self.max_minutes, HARD_MAX_MINUTES))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_settings(cls, row: dict | None) -> "Rules":
        if not row:
            return cls()
        return cls(
            timezone=row.get("timezone") or "Asia/Kolkata",
            open_time=_parse_time(row.get("open_time", "09:00")),
            close_time=_parse_time(row.get("close_time", "19:00")),
            working_days=tuple(int(d) for d in (row.get("working_days") or (1, 2, 3, 4, 5))),
            min_minutes=int(row.get("min_booking_minutes") or 15),
            max_minutes=int(row.get("max_booking_minutes") or 120),
            slot_minutes=int(row.get("slot_minutes") or 15),
            horizon_days=int(row.get("booking_horizon_days") or 30),
            cancel_cutoff_minutes=int(row.get("cancel_cutoff_minutes") or 0),
        )

    def is_working_day(self, day: date) -> bool:
        return day.isoweekday() in self.working_days

    def duration_options(self) -> list[int]:
        step = self.slot_minutes
        options = list(range(self.min_minutes, self.max_minutes + 1, step))
        if self.max_minutes not in options:
            options.append(self.max_minutes)
        return options

    def start_options(self, day: date, now: datetime | None = None) -> list[time]:
        """Slot start times for a local day, excluding ones already in the past."""
        if not self.is_working_day(day):
            return []
        now = now or now_utc()
        tz = self.tz
        out: list[time] = []
        cursor = datetime.combine(day, self.open_time)
        last_start = datetime.combine(day, self.close_time) - timedelta(minutes=self.min_minutes)
        while cursor <= last_start:
            aware = local_datetime(day, cursor.time(), tz)
            if aware is not None and aware >= now:
                out.append(cursor.time())
            cursor += timedelta(minutes=self.slot_minutes)
        return out


@dataclass(frozen=True)
class BookingRequest:
    cabin_id: str
    start_at: datetime
    end_at: datetime
    title: str
    attendees: int
    notes: str | None


def validate_booking(rules: Rules, *, cabin: dict | None, day: date | None, start: time | None,
                     duration: int | None, title: str, attendees: int | None, notes: str | None,
                     now: datetime | None = None) -> tuple[list[str], BookingRequest | None]:
    """Return (errors, request). The request is only built when there are no errors."""
    now = now or now_utc()
    errors: list[str] = []
    title = clean_text(title, 200)
    notes_clean = clean_text(notes, 1100) or None

    if not cabin:
        errors.append("Please choose a cabin.")
    if day is None or start is None or duration is None:
        errors.append("Please choose a date, start time and duration.")
        return errors, None
    if not 3 <= len(title) <= 120:
        errors.append("Meeting purpose must be 3 to 120 characters.")
    if attendees is None or not 1 <= attendees <= 100:
        errors.append("Number of attendees must be between 1 and 100.")
    elif cabin and attendees > int(cabin.get("capacity", 0)):
        errors.append(f"{cabin.get('name', 'This cabin')} seats {cabin.get('capacity')} people.")
    if notes_clean and len(notes_clean) > 1000:
        errors.append("Notes must be 1000 characters or fewer.")
    if cabin and not cabin.get("is_active", True):
        errors.append(f"{cabin.get('name', 'This cabin')} is currently unavailable.")

    if duration < rules.min_minutes:
        errors.append(f"Bookings must be at least {rules.min_minutes} minutes.")
    if duration > rules.max_minutes:
        errors.append(f"Bookings can be at most {rules.max_minutes} minutes.")

    tz = rules.tz
    start_at = local_datetime(day, start, tz)
    if start_at is None:
        errors.append("That time does not exist on this date (clock change). Choose another time.")
        return errors, None
    end_at = start_at.astimezone(UTC) + timedelta(minutes=duration)
    local_end = end_at.astimezone(tz)

    if not rules.is_working_day(day):
        errors.append("Cabins cannot be booked on this day.")
    if (start < rules.open_time or local_end.date() != day or local_end.time() > rules.close_time):
        errors.append(f"Bookings must be between {rules.open_time:%H:%M} and {rules.close_time:%H:%M} "
                      f"({rules.timezone}).")
    if start_at < now:
        errors.append("That start time has already passed.")
    if start_at > now + timedelta(days=rules.horizon_days):
        errors.append(f"Bookings can be made up to {rules.horizon_days} days ahead.")

    if errors:
        return errors, None
    return [], BookingRequest(cabin_id=str(cabin["id"]), start_at=start_at.astimezone(UTC),
                              end_at=end_at, title=title, attendees=int(attendees), notes=notes_clean)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Interval:
    cabin_id: str
    start: datetime
    end: datetime
    kind: str  # 'booking' | 'maintenance'
    is_mine: bool = False
    booking_id: str | None = None
    reference: str | None = None
    title: str | None = None
    booker_name: str | None = None
    reason: str | None = None

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end  # half-open [start, end)


def parse_intervals(rows: Iterable[dict]) -> list[Interval]:
    out = []
    for r in rows or []:
        out.append(Interval(
            cabin_id=str(r["cabin_id"]), start=parse_ts(r["start_at"]), end=parse_ts(r["end_at"]),
            kind=r.get("kind", "booking"), is_mine=bool(r.get("is_mine")),
            booking_id=r.get("booking_id"), reference=r.get("reference"), title=r.get("title"),
            booker_name=r.get("booker_name"), reason=r.get("reason")))
    return sorted(out, key=lambda i: (i.start, i.end))


def is_free(intervals: Iterable[Interval], cabin_id: str, start: datetime, end: datetime) -> bool:
    return not any(i.cabin_id == cabin_id and i.overlaps(start, end) for i in intervals)


STATE_LABELS = {
    "available": "Available",
    "occupied": "Booked",
    "mine": "Your booking",
    "maintenance": "Maintenance",
    "inactive": "Inactive",
}


@dataclass
class CabinStatus:
    state: str
    current: Interval | None = None
    next_free: datetime | None = None

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


def next_available(rules: Rules, intervals: Iterable[Interval], cabin_id: str,
                   after: datetime, search_days: int = 7) -> datetime | None:
    """Earliest instant >= after where the cabin is free for at least min_minutes
    inside office hours. None if nothing in the search window."""
    tz = rules.tz
    busy = sorted((i for i in intervals if i.cabin_id == cabin_id), key=lambda i: i.start)
    cursor = round_up(after, rules.slot_minutes)
    need = timedelta(minutes=rules.min_minutes)
    start_day = cursor.astimezone(tz).date()
    for offset in range(search_days + 1):
        day = start_day + timedelta(days=offset)
        if not rules.is_working_day(day):
            continue
        day_open = local_datetime(day, rules.open_time, tz)
        day_close = local_datetime(day, rules.close_time, tz)
        if day_open is None or day_close is None:
            continue
        candidate = max(cursor, day_open)
        moved = True
        while moved and candidate + need <= day_close:
            moved = False
            for i in busy:
                if i.overlaps(candidate, candidate + need):
                    candidate = round_up(i.end, rules.slot_minutes) if i.end > candidate else candidate
                    moved = True
                    break
        if candidate + need <= day_close:
            return candidate
    return None


def cabin_status(rules: Rules, cabin: dict, intervals: list[Interval],
                 start: datetime, end: datetime) -> CabinStatus:
    """Status of a cabin for the window [start, end)."""
    cabin_id = str(cabin["id"])
    if not cabin.get("is_active", True):
        return CabinStatus("inactive")
    hits = [i for i in intervals if i.cabin_id == cabin_id and i.overlaps(start, end)]
    if not hits:
        return CabinStatus("available", next_free=start)
    current = sorted(hits, key=lambda i: (i.kind != "maintenance", not i.is_mine, i.start))[0]
    state = "maintenance" if current.kind == "maintenance" else ("mine" if current.is_mine else "occupied")
    return CabinStatus(state, current=current, next_free=next_available(rules, intervals, cabin_id, start))


def free_windows(rules: Rules, intervals: Iterable[Interval], cabin_id: str, day: date,
                 now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """Free gaps (>= min duration) for one cabin on one local day."""
    tz = rules.tz
    if not rules.is_working_day(day):
        return []
    lo = local_datetime(day, rules.open_time, tz)
    hi = local_datetime(day, rules.close_time, tz)
    if lo is None or hi is None:
        return []
    if now is not None:
        lo = max(lo, round_up(now, rules.slot_minutes))
    gaps, cursor = [], lo
    for i in sorted((i for i in intervals if i.cabin_id == cabin_id), key=lambda x: x.start):
        if i.end <= cursor or i.start >= hi:
            continue
        if i.start > cursor:
            gaps.append((cursor, min(i.start, hi)))
        cursor = max(cursor, i.end)
    if cursor < hi:
        gaps.append((cursor, hi))
    need = timedelta(minutes=rules.min_minutes)
    return [(a, b) for a, b in gaps if b - a >= need]


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------
class BookingRepository(Protocol):
    def book_cabin(self, cabin_id: str, start_at: datetime, end_at: datetime, title: str,
                   attendees: int, notes: str | None) -> dict: ...

    def cancel_booking(self, booking_id: str, reason: str | None) -> dict: ...


class Notifier(Protocol):
    def process(self, limit: int = 20) -> dict: ...


@dataclass
class BookingOutcome:
    booking: dict
    email_note: str = ""
    warnings: list[str] = field(default_factory=list)


def can_employee_cancel(rules: Rules, booking: dict, now: datetime | None = None) -> bool:
    now = now or now_utc()
    if booking.get("status") != "confirmed":
        return False
    start = parse_ts(booking["start_at"])
    return now <= start - timedelta(minutes=rules.cancel_cutoff_minutes)


class BookingService:
    def __init__(self, repo: BookingRepository, rules: Rules, notifier: Notifier | None = None):
        self.repo = repo
        self.rules = rules
        self.notifier = notifier

    def _send_notifications(self) -> str:
        """Best effort. A failure here never changes the outcome of the booking."""
        if self.notifier is None:
            return ""
        try:
            result = self.notifier.process(limit=10)
        except Exception:  # noqa: BLE001 - logged, never surfaced as a booking failure
            log.exception("Notification dispatch failed; it will be retried")
            return "We couldn't send the e-mail right now; it will be retried automatically."
        if result.get("failed"):
            return "We couldn't send the e-mail right now; it will be retried automatically."
        if result.get("sent"):
            return "A confirmation e-mail is on its way."
        return ""

    def create(self, *, cabin: dict | None, day: date | None, start: time | None, duration: int | None,
               title: str, attendees: int | None, notes: str | None,
               now: datetime | None = None) -> BookingOutcome:
        errors, request = validate_booking(self.rules, cabin=cabin, day=day, start=start, duration=duration,
                                           title=title, attendees=attendees, notes=notes, now=now)
        if errors:
            raise AppError("VALIDATION", " ".join(errors))
        assert request is not None
        # The authoritative check-and-insert happens atomically in the database.
        booking = self.repo.book_cabin(request.cabin_id, request.start_at, request.end_at,
                                       request.title, request.attendees, request.notes)
        if not booking or not booking.get("id"):
            raise AppError("UNEXPECTED", "The server did not confirm the booking. Please check My Bookings.")
        return BookingOutcome(booking=booking, email_note=self._send_notifications())

    def cancel(self, booking_id: str, reason: str | None = None) -> BookingOutcome:
        result = self.repo.cancel_booking(booking_id, clean_text(reason, 300) or None)
        if not result or result.get("status") != "cancelled":
            raise AppError("UNEXPECTED", "The server did not confirm the cancellation.")
        return BookingOutcome(booking=result, email_note=self._send_notifications())


__all__ = [
    "HARD_MAX_MINUTES", "Rules", "BookingRequest", "validate_booking", "Interval", "parse_intervals",
    "is_free", "CabinStatus", "STATE_LABELS", "next_available", "cabin_status", "free_windows",
    "BookingService", "BookingOutcome", "can_employee_cancel", "minutes_between",
]
