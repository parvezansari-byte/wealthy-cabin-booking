"""Time-zone helpers. Instants are always timezone-aware; the office zone is
applied only at the edges (input parsing and display)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a PostgREST timestamptz string into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("naive timestamp received; expected timestamptz")
    return dt.astimezone(UTC)


def local_datetime(day: date, at: time, tz: ZoneInfo) -> datetime | None:
    """Combine a local date and wall-clock time into an aware datetime.

    Returns None for wall-clock times that do not exist (the gap when clocks go
    forward); ambiguous times (clocks go back) resolve to the first occurrence.
    """
    candidate = datetime.combine(day, at).replace(tzinfo=tz, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        return None
    return candidate


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC instants for local midnight..next local midnight (handles 23/25 h days)."""
    start = datetime.combine(day, time(0)).replace(tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0)).replace(tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def round_up(dt: datetime, minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt.second or dt.microsecond else timedelta())
    remainder = dt.minute % minutes
    return dt if remainder == 0 else dt + timedelta(minutes=minutes - remainder)


def fmt_time(dt: datetime | None, tz: ZoneInfo) -> str:
    return "—" if dt is None else dt.astimezone(tz).strftime("%H:%M")


def fmt_date(dt_or_date: datetime | date, tz: ZoneInfo | None = None) -> str:
    if isinstance(dt_or_date, datetime):
        dt_or_date = dt_or_date.astimezone(tz) if tz else dt_or_date
        return dt_or_date.strftime("%a, %d %b %Y")
    return dt_or_date.strftime("%a, %d %b %Y")


def fmt_range(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    return f"{fmt_time(start, tz)}–{fmt_time(end, tz)}"


def minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)
