"""Your reservations: upcoming, completed and cancelled, with search and cancellation."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st

from core.booking_service import can_employee_cancel
from core.timeutil import now_utc, parse_ts
from ui.components import booking_row_html, empty_state, flash, page_header
from ui.context import AppContext
from ui.dialogs import open_booking, open_cancel, render_open_dialogs
from views.shared import guarded, load_cabins


def _filters(ctx: AppContext, bookings: list[dict]):
    tz = ctx.tz
    c1, c2, c3 = st.columns([2, 1.2, 1.6])
    query = c1.text_input("Search", placeholder="Reference or meeting title", key="mb_q")
    cabins = sorted({(b.get("cabins") or {}).get("name", "") for b in bookings} - {""})
    cabin = c2.selectbox("Cabin", ["All cabins", *cabins], key="mb_cabin")
    dates = [parse_ts(b["start_at"]).astimezone(tz).date() for b in bookings]
    rng = c3.date_input("Date range", value=(), key="mb_dates", format="DD/MM/YYYY",
                        min_value=min(dates) if dates else None, max_value=max(dates) if dates else None)
    q = query.strip().lower()

    def keep(b: dict) -> bool:
        if q and q not in (b.get("reference") or "").lower() and q not in (b.get("title") or "").lower():
            return False
        if cabin != "All cabins" and (b.get("cabins") or {}).get("name") != cabin:
            return False
        if isinstance(rng, (tuple, list)) and len(rng) == 2:
            d = parse_ts(b["start_at"]).astimezone(tz).date()
            if not rng[0] <= d <= rng[1]:
                return False
        return True

    return keep


def render(ctx: AppContext) -> None:
    tz, now, rules = ctx.tz, now_utc(), ctx.rules
    page_header("My bookings", "Booking references, history and cancellations. Times in " + rules.timezone + ".")
    flash()
    render_open_dialogs(ctx)
    bookings = guarded(ctx.repo.my_bookings, ctx.access.user_id)

    top1, top2, _ = st.columns([1, 1, 3])
    if top1.button("➕ New booking", type="primary", width="stretch"):
        open_booking(ctx, guarded(load_cabins, ctx))
    if top2.button("↻ Refresh", width="stretch"):
        st.rerun()

    keep = _filters(ctx, bookings)
    rows = [b for b in bookings if keep(b)]
    upcoming = sorted((b for b in rows if b["status"] == "confirmed" and parse_ts(b["end_at"]) > now),
                      key=lambda b: b["start_at"])
    completed = [b for b in rows if b["status"] == "confirmed" and parse_ts(b["end_at"]) <= now]
    cancelled = [b for b in rows if b["status"] == "cancelled"]

    t1, t2, t3 = st.tabs([f"Upcoming ({len(upcoming)})", f"Completed ({len(completed)})",
                          f"Cancelled ({len(cancelled)})"])
    with t1:
        if not upcoming:
            empty_state("No upcoming bookings", "Reserve a cabin from the Availability page or with New booking.")
        if rules.cancel_cutoff_minutes:
            st.caption(f"Bookings can be cancelled up to {rules.cancel_cutoff_minutes} minutes before they start.")
        for b in upcoming:
            started = parse_ts(b["start_at"]) <= now
            left, right = st.columns([5, 1.2], vertical_alignment="center")
            left.markdown(booking_row_html(b, tz, "occupied" if started else "mine",
                                           "In progress" if started else "Upcoming"), unsafe_allow_html=True)
            if can_employee_cancel(rules, b, now):
                if right.button("Cancel", key=f"mb_cancel_{b['id']}", width="stretch"):
                    open_cancel(b)
            else:
                cutoff = parse_ts(b["start_at"]) - timedelta(minutes=rules.cancel_cutoff_minutes)
                right.caption("Can't cancel now" if now > cutoff else "")
    with t2:
        if not completed:
            empty_state("No completed bookings yet")
        for b in sorted(completed, key=lambda b: b["start_at"], reverse=True):
            st.markdown(booking_row_html(b, tz, "available", "Completed"), unsafe_allow_html=True)
    with t3:
        if not cancelled:
            empty_state("No cancelled bookings")
        for b in sorted(cancelled, key=lambda b: b["start_at"], reverse=True):
            st.markdown(booking_row_html(b, tz, "inactive", "Cancelled"), unsafe_allow_html=True)
