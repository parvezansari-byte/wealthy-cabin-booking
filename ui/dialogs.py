"""Booking form and cancellation confirmation, shown as modal dialogs."""
from __future__ import annotations

from datetime import date, time, timedelta

import streamlit as st

from core.booking_service import is_free, parse_intervals
from core.errors import AppError
from core.timeutil import day_bounds, fmt_date, fmt_range, local_datetime, now_utc, parse_ts
from ui.context import AppContext


def _default_slot(ctx: AppContext) -> tuple[date, time | None]:
    rules, now = ctx.rules, now_utc()
    day = now.astimezone(ctx.tz).date()
    for _ in range(rules.horizon_days + 1):
        options = rules.start_options(day, now)
        if options:
            return day, options[0]
        day += timedelta(days=1)
    return now.astimezone(ctx.tz).date(), None


BOOKING_KEY, CANCEL_KEY = "open_booking_dialog", "open_cancel_dialog"


def _dismiss_booking() -> None:
    st.session_state.pop(BOOKING_KEY, None)


def _dismiss_cancel() -> None:
    st.session_state.pop(CANCEL_KEY, None)


@st.dialog("Book a cabin", width="large", on_dismiss=_dismiss_booking)
def booking_dialog(ctx: AppContext, cabins: list[dict], cabin_id: str | None = None,
                   day: date | None = None, start: time | None = None, duration: int | None = None) -> None:
    rules, tz, now = ctx.rules, ctx.tz, now_utc()
    active = [c for c in cabins if c.get("is_active")]
    if not active:
        st.warning("No cabins are available for booking right now.")
        return

    default_day, default_start = _default_slot(ctx)
    day = day or default_day
    ids = [str(c["id"]) for c in active]
    idx = ids.index(str(cabin_id)) if cabin_id and str(cabin_id) in ids else 0

    st.caption(f"Booking as **{ctx.access.display_name}** ({ctx.access.email}) · times in {rules.timezone}")
    c1, c2 = st.columns(2)
    cabin = c1.selectbox("Cabin", active, index=idx, key="bk_cabin",
                         format_func=lambda c: f"{c['name']} · seats {c['capacity']}")
    today = now.astimezone(tz).date()
    chosen_day = c2.date_input("Meeting date", value=max(day, today), min_value=today,
                               max_value=today + timedelta(days=rules.horizon_days), key="bk_day",
                               format="DD/MM/YYYY")

    starts = rules.start_options(chosen_day, now)
    c3, c4, c5 = st.columns(3)
    if not starts:
        c3.selectbox("Start time", ["No slots"], disabled=True, key="bk_start_none")
        st.info("There are no bookable times left on this date. Office hours are "
                f"{rules.open_time:%H:%M}–{rules.close_time:%H:%M} on working days.")
        return
    start_default = start if start in starts else (default_start if default_start in starts else starts[0])
    chosen_start = c3.selectbox("Start time", starts, index=starts.index(start_default), key="bk_start",
                                format_func=lambda t: t.strftime("%H:%M"))
    durations = rules.duration_options()
    dur_default = duration if duration in durations else (60 if 60 in durations else durations[0])
    chosen_duration = c4.selectbox("Duration", durations, index=durations.index(dur_default), key="bk_dur",
                                   format_func=lambda m: f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min")
    start_at = local_datetime(chosen_day, chosen_start, tz)
    end_at = start_at + timedelta(minutes=chosen_duration) if start_at else None
    c5.text_input("Ends at (calculated)", value=end_at.astimezone(tz).strftime("%H:%M") if end_at else "—",
                  disabled=True, key="bk_end")

    # Live availability hint (the database re-checks atomically on submit).
    if start_at and end_at:
        try:
            lo, hi = day_bounds(chosen_day, tz)
            intervals = parse_intervals(ctx.repo.availability(lo, hi))
            if not is_free(intervals, str(cabin["id"]), start_at, end_at):
                st.warning(f"{cabin['name']} is not free for {fmt_range(start_at, end_at, tz)}. "
                           "Choose another time or cabin.")
            else:
                st.success(f"{cabin['name']} is free on {fmt_date(chosen_day)}, {fmt_range(start_at, end_at, tz)}.")
        except AppError as err:
            st.caption(f"Live availability unavailable: {err.message}")

    title = st.text_input("Meeting title / purpose *", max_chars=120, key="bk_title",
                          placeholder="e.g. Weekly product sync")
    attendees = st.number_input("Number of attendees *", min_value=1, max_value=int(cabin["capacity"]),
                                value=min(2, int(cabin["capacity"])), step=1, key="bk_att")
    notes = st.text_area("Notes (optional, visible only to you and administrators)", max_chars=1000,
                         key="bk_notes", height=80)

    b1, b2 = st.columns([1, 1])
    if b2.button("Cancel", width="stretch", key="bk_close"):
        _dismiss_booking()
        st.rerun()
    if b1.button("Confirm booking", type="primary", width="stretch", key="bk_submit"):
        try:
            with st.spinner("Reserving your cabin…"):
                outcome = ctx.booking_service.create(
                    cabin=cabin, day=chosen_day, start=chosen_start, duration=int(chosen_duration),
                    title=title, attendees=int(attendees), notes=notes)
        except AppError as err:
            st.error(err.message)
            return
        b = outcome.booking
        s, e = parse_ts(b["start_at"]), parse_ts(b["end_at"])
        msg = (f"Booked {b['cabin_name']} on {fmt_date(s, tz)}, {fmt_range(s, e, tz)}. "
               f"Reference **{b['reference']}**. {outcome.email_note}").strip()
        st.session_state.flash = ("success", msg)
        st.session_state.just_booked = {**b, "cabin_location": cabin.get("location")}
        _dismiss_booking()
        st.rerun()


@st.dialog("Cancel this booking?", on_dismiss=_dismiss_cancel)
def cancel_dialog(ctx: AppContext, booking: dict, as_admin: bool = False) -> None:
    tz = ctx.tz
    s, e = parse_ts(booking["start_at"]), parse_ts(booking["end_at"])
    cabin = (booking.get("cabins") or {}).get("name", "Cabin")
    st.markdown(f"**{booking['reference']}** · {cabin}  \n{fmt_date(s, tz)}, {fmt_range(s, e, tz)}  \n"
                f"_{booking.get('title', '')}_")
    if as_admin and str(booking.get("user_id")) != ctx.access.user_id:
        st.info("The booking owner will be notified that an administrator cancelled it.")
    reason = st.text_input("Reason (optional)", max_chars=300, key=f"cx_reason_{booking['id']}")
    c1, c2 = st.columns(2)
    if c2.button("Keep booking", width="stretch", key=f"cx_keep_{booking['id']}"):
        _dismiss_cancel()
        st.rerun()
    if c1.button("Yes, cancel it", type="primary", width="stretch", key=f"cx_go_{booking['id']}"):
        try:
            with st.spinner("Cancelling…"):
                outcome = ctx.booking_service.cancel(str(booking["id"]), reason)
        except AppError as err:
            st.error(err.message)
            return
        st.session_state.flash = ("success", f"Booking {booking['reference']} cancelled. {outcome.email_note}".strip())
        _dismiss_cancel()
        st.rerun()


def default_slot(ctx: AppContext) -> tuple[date, time | None]:
    return _default_slot(ctx)


# Dialog visibility is kept in session state (and cleared on submit, Cancel or the
# close button), so the dialog survives any full-page rerun until it is finished.
def open_booking(ctx: AppContext, cabins: list[dict] | None = None, **kwargs) -> None:
    """Open the booking dialog with fresh widget state (no values left over from last time)."""
    for key in [k for k in st.session_state if str(k).startswith("bk_")]:
        del st.session_state[key]
    st.session_state.pop(CANCEL_KEY, None)
    st.session_state[BOOKING_KEY] = kwargs
    st.rerun()


def open_cancel(booking: dict, as_admin: bool = False) -> None:
    st.session_state.pop(BOOKING_KEY, None)
    st.session_state[CANCEL_KEY] = {"booking": booking, "as_admin": as_admin}
    st.rerun()


def render_open_dialogs(ctx: AppContext, cabins: list[dict] | None = None) -> None:
    """Call once near the end of every page that can open a dialog."""
    if BOOKING_KEY in st.session_state:
        booking_dialog(ctx, cabins if cabins is not None else ctx.repo.list_cabins(),
                       **st.session_state[BOOKING_KEY])
    elif CANCEL_KEY in st.session_state:
        spec = st.session_state[CANCEL_KEY]
        cancel_dialog(ctx, spec["booking"], as_admin=spec["as_admin"])


def just_booked_panel(ctx: AppContext) -> None:
    """After a successful booking, offer to add it to the person's calendar."""
    from ui.components import add_to_calendar

    b = st.session_state.pop("just_booked", None)
    if not b:
        return
    c1, _ = st.columns([1, 3])
    with c1:
        add_to_calendar(b, b.get("cabin_name", "Cabin"), b.get("cabin_location"), ctx.app_name,
                        ctx.cfg.app_base_url, key=f"new_{b['id']}", label="📅 Add this booking to your calendar")
