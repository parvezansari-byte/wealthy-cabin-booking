"""Employee home: who you are, what's free right now, and your upcoming meetings."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st

from core.booking_service import cabin_status
from core.security import esc
from core.timeutil import fmt_date, fmt_range, now_utc, parse_ts, round_up
from ui.components import booking_row_html, cabin_card, empty_state, flash, legend, metric
from ui.context import AppContext
from ui.dialogs import open_booking, render_open_dialogs
from views.shared import guarded, load_cabins, load_intervals


def _greeting(hour: int) -> str:
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


def render(ctx: AppContext) -> None:
    tz, rules, now = ctx.tz, ctx.rules, now_utc()
    local_now = now.astimezone(tz)
    a = ctx.access
    dept = f" · {a.department}" if a.department else ""
    st.markdown(
        f'<div class="wcb-hero"><h1>{esc(_greeting(local_now.hour))}, {esc(a.display_name)}</h1>'
        f'<p>{esc(a.email)}{esc(dept)} · {esc("Administrator" if a.is_admin else "Employee")}</p>'
        f'<p>{esc(fmt_date(local_now))} · {local_now:%H:%M} · Office time zone: {esc(rules.timezone)}</p></div>',
        unsafe_allow_html=True)
    flash()
    render_open_dialogs(ctx)

    cabins = guarded(load_cabins, ctx)
    # Look 8 days ahead so "next available" can be computed for every card.
    intervals = guarded(load_intervals, ctx, now - timedelta(hours=3), now + timedelta(days=8))
    mine = guarded(ctx.repo.my_bookings, a.user_id)

    window_end = now + timedelta(minutes=rules.min_minutes)
    statuses = {str(c["id"]): cabin_status(rules, c, intervals, now, window_end) for c in cabins}
    in_hours = rules.is_working_day(local_now.date()) and rules.open_time <= local_now.time() < rules.close_time
    available = sum(1 for s in statuses.values() if s.state == "available") if in_hours else 0
    occupied = sum(1 for s in statuses.values() if s.state in ("occupied", "mine", "maintenance"))
    upcoming = sorted((b for b in mine if b["status"] == "confirmed" and parse_ts(b["end_at"]) > now),
                      key=lambda b: b["start_at"])
    history = [b for b in mine if b["status"] == "cancelled" or parse_ts(b["end_at"]) <= now]

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        metric("Total cabins", len(cabins), f"{sum(1 for c in cabins if c.get('is_active'))} active")
    with m2:
        metric("Available now", available, "inside office hours" if in_hours else "office is closed")
    with m3:
        metric("Occupied now", occupied, "booked or maintenance")
    with m4:
        metric("My upcoming", len(upcoming), "confirmed bookings")

    st.write("")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("⚡ Quick Book", type="primary", width="stretch"):
        open_booking(ctx, cabins)
    if c2.button("↻ Refresh", width="stretch"):
        st.rerun()

    left, right = st.columns([1.7, 1], gap="large")
    with left:
        st.subheader("Cabins right now")
        legend()
        f1, f2, f3 = st.columns([2, 1, 1.3])
        query = f1.text_input("Search cabins", placeholder="Name, location or equipment",
                              label_visibility="collapsed")
        min_cap = f2.number_input("Min seats", min_value=1, max_value=100, value=1, label_visibility="collapsed",
                                  help="Minimum seating capacity")
        status_filter = f3.selectbox("Status", ["All statuses", "Available", "Booked", "Your booking",
                                                "Maintenance", "Inactive"], label_visibility="collapsed")
        q = query.strip().lower()
        shown = []
        for c in cabins:
            s = statuses[str(c["id"])]
            hay = " ".join([c["name"], c.get("location") or "", *(c.get("equipment") or [])]).lower()
            if q and q not in hay:
                continue
            if int(c["capacity"]) < min_cap:
                continue
            if status_filter != "All statuses" and s.label != status_filter:
                continue
            shown.append(c)
        if not shown:
            empty_state("No cabins match your filters", "Try clearing the search or lowering the seat count.")
        cols = st.columns(2)
        label = f"now ({fmt_range(now, window_end, tz)})"
        for i, c in enumerate(shown):
            with cols[i % 2]:
                s = statuses[str(c["id"])]
                cabin_card(c, s, tz, label)
                start_day, start_time = None, None
                if s.next_free is not None:
                    nf = round_up(s.next_free, rules.slot_minutes).astimezone(tz)
                    start_day, start_time = nf.date(), nf.time()
                if st.button(f"Book {c['name']}", key=f"dash_book_{c['id']}", width="stretch",
                             disabled=s.state == "inactive",
                             help=None if s.state != "inactive" else "This cabin is inactive"):
                    open_booking(ctx, cabins, cabin_id=str(c["id"]), day=start_day, start=start_time)

    with right:
        st.subheader("My bookings")
        t1, t2 = st.tabs([f"Upcoming ({len(upcoming)})", "History"])
        with t1:
            if not upcoming:
                empty_state("No upcoming bookings", "Use Quick Book to reserve a cabin.")
            for b in upcoming[:6]:
                start = parse_ts(b["start_at"])
                state = "mine" if start > now else "occupied"
                st.markdown(booking_row_html(b, tz, state, "Upcoming" if start > now else "In progress"),
                            unsafe_allow_html=True)
        with t2:
            if not history:
                empty_state("No booking history yet")
            for b in sorted(history, key=lambda b: b["start_at"], reverse=True)[:6]:
                cancelled = b["status"] == "cancelled"
                st.markdown(booking_row_html(b, tz, "inactive" if cancelled else "available",
                                             "Cancelled" if cancelled else "Completed"), unsafe_allow_html=True)
        if "my_bookings" in ctx.pages:
            st.page_link(ctx.pages["my_bookings"], label="See all my bookings →")
