"""Utilisation reporting and CSV export (pure functions, unit tested)."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Iterable

from core.booking_service import Rules
from core.security import csv_safe
from core.timeutil import day_bounds, fmt_date, fmt_time, local_datetime, parse_ts


def office_minutes(rules: Rules, start_day: date, end_day: date) -> int:
    """Bookable minutes per cabin for working days in [start_day, end_day]."""
    total, day = 0, start_day
    while day <= end_day:
        if rules.is_working_day(day):
            lo = local_datetime(day, rules.open_time, rules.tz)
            hi = local_datetime(day, rules.close_time, rules.tz)
            if lo and hi:
                total += int((hi - lo).total_seconds() // 60)
        day += timedelta(days=1)
    return total


def utilization(rules: Rules, cabins: list[dict], bookings: Iterable[dict],
                start_day: date, end_day: date) -> list[dict]:
    """Per-cabin booked minutes (confirmed bookings, clipped to the range)."""
    tz = rules.tz
    range_start, _ = day_bounds(start_day, tz)
    range_end, _ = day_bounds(end_day + timedelta(days=1), tz)
    capacity_minutes = office_minutes(rules, start_day, end_day)
    stats = {str(c["id"]): {"cabin": c["name"], "code": c["code"], "bookings": 0, "booked_minutes": 0,
                            "cancelled": 0} for c in cabins}
    for b in bookings:
        row = stats.get(str(b["cabin_id"]))
        if row is None:
            continue
        if b.get("status") == "cancelled":
            row["cancelled"] += 1
            continue
        start, end = parse_ts(b["start_at"]), parse_ts(b["end_at"])
        start, end = max(start, range_start), min(end, range_end)
        if end <= start:
            continue
        row["bookings"] += 1
        row["booked_minutes"] += int((end - start).total_seconds() // 60)
    out = []
    for row in stats.values():
        pct = (100.0 * row["booked_minutes"] / capacity_minutes) if capacity_minutes else 0.0
        out.append({**row, "available_minutes": capacity_minutes, "utilization_pct": round(pct, 1)})
    return sorted(out, key=lambda r: r["code"])


BOOKING_COLUMNS = ["reference", "cabin", "date", "start", "end", "duration_min", "title", "attendees",
                   "employee", "employee_email", "status", "cancelled_at", "cancel_reason", "created_at"]


def bookings_csv(rows: Iterable[dict], rules: Rules, include_notes: bool = False) -> str:
    tz = rules.tz
    buf = io.StringIO()
    columns = BOOKING_COLUMNS + (["notes"] if include_notes else [])
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for b in rows:
        start, end = parse_ts(b["start_at"]), parse_ts(b["end_at"])
        owner = b.get("owner") or {}
        cabin = b.get("cabins") or {}
        cancelled = parse_ts(b.get("cancelled_at"))
        record = {
            "reference": b.get("reference"), "cabin": cabin.get("name"),
            "date": start.astimezone(tz).date().isoformat(),
            "start": fmt_time(start, tz), "end": fmt_time(end, tz),
            "duration_min": int((end - start).total_seconds() // 60),
            "title": b.get("title"), "attendees": b.get("attendees"),
            "employee": owner.get("full_name"), "employee_email": owner.get("email"),
            "status": b.get("status"),
            "cancelled_at": cancelled.astimezone(tz).isoformat(timespec="minutes") if cancelled else "",
            "cancel_reason": b.get("cancel_reason") or "",
            "created_at": parse_ts(b["created_at"]).astimezone(tz).isoformat(timespec="minutes")
            if b.get("created_at") else "",
            "notes": b.get("notes") or "",
        }
        writer.writerow({k: csv_safe(v) for k, v in record.items()})
    return buf.getvalue()


def rows_csv(rows: Iterable[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: csv_safe(r.get(c) if not isinstance(r.get(c), (dict, list)) else str(r.get(c)))
                         for c in columns})
    return buf.getvalue()


__all__ = ["office_minutes", "utilization", "bookings_csv", "rows_csv", "fmt_date"]
