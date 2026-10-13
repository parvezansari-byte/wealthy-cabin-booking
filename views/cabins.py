"""Cabin availability: pick a date/time/duration, see all 10 cabins, book, and view the day timeline."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st

from core.booking_service import cabin_status
from core.timeutil import day_bounds, fmt_date, fmt_range, local_datetime, now_utc
from ui.components import cabin_card, empty_state, flash, legend, page_header, timeline_chart
from ui.context import AppContext
from ui.dialogs import default_slot, open_booking, render_open_dialogs
from views.shared import guarded, load_cabins, load_intervals


def _board(ctx: AppContext, day, start, duration, min_cap, equipment, only_available) -> None:
    tz, rules, now = ctx.tz, ctx.rules, now_utc()
    cabins = guarded(load_cabins, ctx)
    lo, hi = day_bounds(day, tz)
    # Through the end of the next 7 days so "next available" can roll over to later days.
    intervals = guarded(load_intervals, ctx, lo, hi + timedelta(days=7))

    start_at = local_datetime(day, start, tz)
    if start_at is None:
        st.warning("That time does not exist on this date because of a clock change. Pick another time.")
        return
    end_at = start_at + timedelta(minutes=duration)
    window = f"{fmt_date(day)}, {fmt_range(start_at, end_at, tz)}"

    rows = []
    for c in cabins:
        s = cabin_status(rules, c, intervals, start_at, end_at)
        if int(c["capacity"]) < min_cap:
            continue
        if equipment and not set(equipment).issubset(set(c.get("equipment") or [])):
            continue
        if only_available and s.state != "available":
            continue
        rows.append((c, s))

    free = sum(1 for _, s in rows if s.state == "available")
    st.markdown(f"**{free} of {len(rows)} shown cabins are free** for {window}. "
                f"_Updated {now.astimezone(tz):%H:%M:%S}_")
    legend()
    if not rows:
        empty_state("No cabins match these filters", "Try another time, fewer seats or fewer equipment filters.")
    cols = st.columns(3)
    for i, (c, s) in enumerate(rows):
        with cols[i % 3]:
            cabin_card(c, s, tz, window)
            can_book = s.state == "available"
            label = f"Book {c['name']}" if can_book else f"{c['name']}: {s.label}"
            if st.button(label, key=f"av_book_{c['id']}", type="primary" if can_book else "secondary",
                         width="stretch", disabled=not can_book):
                open_booking(ctx, cabins, cabin_id=str(c["id"]), day=day, start=start, duration=duration)
            st.write("")

    st.subheader("Day timeline")
    st.caption("Reserved and free slots for every cabin. Hover a bar for details. "
               "Other employees' meeting details are private.")
    day_intervals = [i for i in intervals if i.start < hi and i.end > lo]
    timeline_chart(rules, cabins, day_intervals, day, now)


def render(ctx: AppContext) -> None:
    rules, tz, now = ctx.rules, ctx.tz, now_utc()
    page_header("Cabin availability", f"All times are in {rules.timezone}. Bookings can last "
                f"{rules.min_minutes}–{rules.max_minutes} minutes, {rules.open_time:%H:%M}–{rules.close_time:%H:%M}.")
    flash()
    render_open_dialogs(ctx)

    d_day, d_start = default_slot(ctx)
    today = now.astimezone(tz).date()
    c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
    day = c1.date_input("Date", value=d_day, min_value=today, max_value=today + timedelta(days=rules.horizon_days),
                        format="DD/MM/YYYY", key="av_day")
    starts = rules.start_options(day, now)
    if not starts:
        st.info(f"No bookable times on {fmt_date(day)}. Office hours are {rules.open_time:%H:%M}–"
                f"{rules.close_time:%H:%M} on working days, and past times are hidden.")
        return
    start = c2.selectbox("Start time", starts, index=starts.index(d_start) if d_start in starts else 0,
                         format_func=lambda t: t.strftime("%H:%M"), key="av_start")
    durations = rules.duration_options()
    duration = c3.selectbox("Duration", durations, index=durations.index(60) if 60 in durations else 0,
                            format_func=lambda m: f"{m} min", key="av_dur")
    min_cap = c4.number_input("Min. seats", min_value=1, max_value=100, value=1, key="av_cap")

    c5, c6, c7, c8 = st.columns([2, 1, 1, 1])
    all_equipment = sorted({e for c in guarded(load_cabins, ctx) for e in (c.get("equipment") or [])})
    equipment = c5.multiselect("Equipment", all_equipment, key="av_eq", placeholder="Any equipment")
    only_available = c6.toggle("Only available", key="av_only")
    auto = c7.toggle("Auto-refresh", value=False, key="av_auto", help="Refresh availability every 60 seconds")
    if c8.button("↻ Refresh", width="stretch"):
        st.rerun()

    args = (ctx, day, start, int(duration), int(min_cap), equipment, only_available)
    if auto:
        st.fragment(run_every=60)(_board)(*args)
    else:
        _board(*args)
