"""Supabase access layer.

* ``create_user_client`` — anon key + the signed-in user's JWT. Every query it
  makes is subject to Row Level Security as that user. This is the ONLY client
  the UI uses for data.
* ``create_service_client`` — service-role key, bypasses RLS. Used solely by the
  notification worker (core/notifications.py, scripts/process_notifications.py).
  It is created on the server and never reaches a browser.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from core.errors import AppError, from_postgrest

log = logging.getLogger(__name__)

PAGE = 1000  # Supabase's default PostgREST max-rows
SCHEMA = "cabins"  # all app tables/functions; add it to Supabase › API › Exposed schemas


def create_user_client(url: str, anon_key: str):
    from supabase import ClientOptions, create_client

    return create_client(url, anon_key, options=ClientOptions(
        schema=SCHEMA, auto_refresh_token=True, persist_session=False, postgrest_client_timeout=15))


def create_service_client(url: str, service_role_key: str):
    from supabase import ClientOptions, create_client

    if not service_role_key:
        raise AppError("CONFIG", "SUPABASE_SERVICE_ROLE_KEY is not configured.")
    return create_client(url, service_role_key, options=ClientOptions(
        schema=SCHEMA, auto_refresh_token=False, persist_session=False, postgrest_client_timeout=15))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("timestamps sent to the database must be timezone-aware")
    return dt.isoformat()


def _run(fn: Callable[[], Any]) -> Any:
    """Execute a PostgREST call, converting every failure into a safe AppError."""
    try:
        from postgrest.exceptions import APIError
    except ImportError:  # pragma: no cover
        APIError = ()  # type: ignore[assignment]
    try:
        return fn()
    except AppError:
        raise
    except APIError as exc:  # type: ignore[misc]
        raise from_postgrest(exc) from None
    except Exception as exc:  # network errors, timeouts, JSON errors …
        log.exception("Database request failed: %s", type(exc).__name__)
        raise AppError("NETWORK", "We couldn't reach the booking database. "
                                  "Nothing was changed — please try again.") from None


class Repository:
    """All reads/writes the UI performs, as the signed-in user (RLS applies)."""

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------ helpers
    def _rpc(self, fn: str, params: dict | None = None) -> Any:
        return _run(lambda: self.client.rpc(fn, params or {}).execute().data)

    def _all(self, build: Callable[[], Any], limit: int = 20000) -> list[dict]:
        rows: list[dict] = []
        start = 0
        while start < limit:
            chunk = _run(lambda: build().range(start, start + PAGE - 1).execute().data) or []
            rows.extend(chunk)
            if len(chunk) < PAGE:
                break
            start += PAGE
        return rows

    # ------------------------------------------------------------------ identity
    def get_my_access(self) -> dict | None:
        return self._rpc("get_my_access")

    def get_branding(self) -> dict:
        return self._rpc("get_public_branding") or {}

    def update_my_profile(self, user_id: str, full_name: str, department: str | None) -> None:
        _run(lambda: self.client.table("profiles")
             .update({"full_name": full_name, "department": department or None})
             .eq("id", user_id).execute())

    # ------------------------------------------------------------------ reference data
    def get_settings(self) -> dict:
        rows = _run(lambda: self.client.table("office_settings").select("*").limit(1).execute().data)
        if not rows:
            raise AppError("EMPLOYEE_NOT_AUTHORIZED")
        return rows[0]

    def list_cabins(self) -> list[dict]:
        return _run(lambda: self.client.table("cabins").select("*").order("sort_order").order("code")
                    .execute().data) or []

    def availability(self, start: datetime, end: datetime) -> list[dict]:
        return self._rpc("get_availability", {"p_from": _iso(start), "p_to": _iso(end)}) or []

    # ------------------------------------------------------------------ bookings
    def book_cabin(self, cabin_id: str, start_at: datetime, end_at: datetime, title: str,
                   attendees: int, notes: str | None) -> dict:
        return self._rpc("book_cabin", {
            "p_cabin_id": cabin_id, "p_start_at": _iso(start_at), "p_end_at": _iso(end_at),
            "p_title": title, "p_attendees": attendees, "p_notes": notes})

    def cancel_booking(self, booking_id: str, reason: str | None) -> dict:
        return self._rpc("cancel_booking", {"p_booking_id": booking_id, "p_reason": reason})

    def my_bookings(self, user_id: str) -> list[dict]:
        return self._all(lambda: self.client.table("bookings")
                         .select("*, cabins(name, code, location)")
                         .eq("user_id", user_id).order("start_at", desc=True))

    # ------------------------------------------------------------------ admin
    def admin_bookings(self, start: datetime, end: datetime) -> list[dict]:
        return self._all(lambda: self.client.table("bookings")
                         .select("*, cabins(name, code), owner:profiles!bookings_user_id_fkey(full_name, email)")
                         .gte("start_at", _iso(start)).lt("start_at", _iso(end))
                         .order("start_at"))

    def list_profiles(self) -> list[dict]:
        return self._all(lambda: self.client.table("profiles").select("*").order("created_at", desc=True))

    def set_employee_status(self, user_id: str, status: str, cancel_future: bool = False) -> dict:
        return self._rpc("admin_set_employee_status",
                         {"p_user_id": user_id, "p_status": status, "p_cancel_future": cancel_future})

    def set_role(self, user_id: str, role: str) -> dict:
        return self._rpc("admin_set_role", {"p_user_id": user_id, "p_role": role})

    def list_allowlist(self) -> list[dict]:
        return self._all(lambda: self.client.table("employee_allowlist").select("*").order("email"))

    def add_allowlist(self, email: str, note: str | None) -> None:
        _run(lambda: self.client.table("employee_allowlist")
             .insert({"email": email, "note": note or None}).execute())

    def remove_allowlist(self, email: str) -> None:
        _run(lambda: self.client.table("employee_allowlist").delete().eq("email", email).execute())

    def update_cabin(self, cabin_id: str, changes: dict) -> None:
        allowed = {"name", "location", "capacity", "equipment", "is_active", "sort_order"}
        payload = {k: v for k, v in changes.items() if k in allowed}
        _run(lambda: self.client.table("cabins").update(payload).eq("id", cabin_id).execute())

    def update_settings(self, changes: dict) -> None:
        allowed = {"app_name", "company_name", "logo_url", "timezone", "open_time", "close_time",
                   "working_days", "min_booking_minutes", "max_booking_minutes", "slot_minutes",
                   "booking_horizon_days", "cancel_cutoff_minutes", "reminder_minutes_before",
                   "show_booker_names", "access_policy"}
        payload = {k: v for k, v in changes.items() if k in allowed}
        _run(lambda: self.client.table("office_settings").update(payload).eq("id", "true").execute())

    def list_maintenance(self, since: datetime) -> list[dict]:
        return self._all(lambda: self.client.table("maintenance_blocks")
                         .select("*, cabins(name, code)").eq("status", "active")
                         .gte("end_at", _iso(since)).order("start_at"))

    def create_maintenance(self, cabin_id: str, start_at: datetime, end_at: datetime, reason: str,
                           cancel_conflicts: bool) -> dict:
        return self._rpc("admin_create_maintenance", {
            "p_cabin_id": cabin_id, "p_start_at": _iso(start_at), "p_end_at": _iso(end_at),
            "p_reason": reason, "p_cancel_conflicts": cancel_conflicts})

    def remove_maintenance(self, block_id: str) -> dict:
        return self._rpc("admin_remove_maintenance", {"p_id": block_id})

    def audit_logs(self, limit: int = 500, action_prefix: str | None = None) -> list[dict]:
        def build():
            q = self.client.table("audit_logs").select("*").order("occurred_at", desc=True)
            if action_prefix:
                q = q.like("action", f"{action_prefix}%")
            return q
        return self._all(build, limit=limit)[:limit]

    def notifications(self, status: str | None = None, limit: int = 500) -> list[dict]:
        def build():
            q = self.client.table("notification_logs").select("*, bookings(reference)") \
                .order("created_at", desc=True)
            if status:
                q = q.eq("status", status)
            return q
        return self._all(build, limit=limit)[:limit]

    def retry_failed_notifications(self) -> int:
        return int(self._rpc("admin_retry_failed_notifications") or 0)
