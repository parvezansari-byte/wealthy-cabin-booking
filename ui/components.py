"""Reusable UI pieces. Every dynamic value that goes into HTML passes through esc()."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import streamlit as st

from core.booking_service import STATE_LABELS, CabinStatus, Interval, Rules, free_windows
from core.security import esc
from core.timeutil import fmt_date, fmt_range, fmt_time, local_datetime
from ui.theme import ICONS, PALETTE


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(f'<div class="wcb-page-title" role="heading" aria-level="1">{esc(title)}</div>'
                + (f'<div class="wcb-page-sub">{esc(subtitle)}</div>' if subtitle else ""),
                unsafe_allow_html=True)


def badge(state: str, label: str | None = None) -> str:
    fg, bg, border = PALETTE[state]
    text = label or STATE_LABELS[state]
    return (f'<span class="wcb-badge" style="color:{fg};background:{bg};border-color:{border}">'
            f'<span aria-hidden="true">{ICONS[state]}</span>{esc(text)}</span>')


def legend() -> None:
    st.markdown('<div class="wcb-legend" role="list" aria-label="Status legend">'
                + "".join(f'<span role="listitem">{badge(s)}</span>' for s in PALETTE) + "</div>",
                unsafe_allow_html=True)


def metric(label: str, value: object, sub: str = "") -> None:
    st.markdown(f'<div class="wcb-metric"><div class="k">{esc(label)}</div><div class="v">{esc(value)}</div>'
                f'<div class="s">{esc(sub)}</div></div>', unsafe_allow_html=True)


def empty_state(title: str, body: str = "") -> None:
    st.markdown(f'<div class="wcb-empty"><div class="t">{esc(title)}</div><div>{esc(body)}</div></div>',
                unsafe_allow_html=True)


def flash() -> None:
    """Show a one-time message stored before st.rerun()."""
    msg = st.session_state.pop("flash", None)
    if not msg:
        return
    kind, text = msg
    getattr(st, kind, st.info)(text)


def cabin_card(cabin: dict, status: CabinStatus, tz: ZoneInfo, window_label: str) -> None:
    fg, _bg, _border = PALETTE[status.state]
    lines = []
    cur: Interval | None = status.current
    if status.state == "available":
        lines.append(f"<b>Free</b> for {esc(window_label)}")
    elif cur is not None:
        what = "Maintenance" if cur.kind == "maintenance" else ("Your meeting" if cur.is_mine else "Booked")
        lines.append(f"<b>{esc(what)}</b> {esc(fmt_range(cur.start, cur.end, tz))}")
        if cur.is_mine and cur.title:
            lines.append(f"“{esc(cur.title)}”")
        elif cur.kind == "maintenance" and cur.reason:
            lines.append(esc(cur.reason))
        elif cur.booker_name:
            lines.append(f"by {esc(cur.booker_name)}")
    if status.state in ("occupied", "mine", "maintenance"):
        nxt = status.next_free
        nxt_text = "none in the next 7 days" if nxt is None else (
            fmt_time(nxt, tz) if nxt.astimezone(tz).date() == (cur.start.astimezone(tz).date() if cur else None)
            else f"{fmt_date(nxt, tz)} {fmt_time(nxt, tz)}")
        lines.append(f"Next available: <b>{esc(nxt_text)}</b>")
    if status.state == "inactive":
        lines.append("Not available for booking")

    chips = "".join(f'<span class="wcb-chip">{esc(e)}</span>' for e in (cabin.get("equipment") or []))
    location = f" · {esc(cabin['location'])}" if cabin.get("location") else ""
    st.markdown(
        f'<div class="wcb-card" style="border-top-color:{fg}" '
        f'aria-label="{esc(cabin["name"])}: {esc(status.label)}">'
        f'<div class="top"><div><div class="code">{esc(cabin["code"])}</div>'
        f'<p class="name">{esc(cabin["name"])}</p>'
        f'<div class="meta">👥 Seats {esc(cabin["capacity"])}{location}</div></div>{badge(status.state)}</div>'
        + "".join(f'<div class="line">{line}</div>' for line in lines)
        + (f'<div class="wcb-chips">{chips}</div>' if chips else "")
        + "</div>", unsafe_allow_html=True)


def booking_row_html(b: dict, tz: ZoneInfo, state: str, state_label: str) -> str:
    from core.timeutil import parse_ts

    start, end = parse_ts(b["start_at"]), parse_ts(b["end_at"])
    cabin = (b.get("cabins") or {}).get("name", "Cabin")
    extra = ""
    if b.get("status") == "cancelled" and b.get("cancel_reason"):
        extra = f'<div class="when">Reason: {esc(b["cancel_reason"])}</div>'
    return (f'<div class="wcb-row"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">'
            f'<span class="ref">{esc(b.get("reference"))}</span>{badge(state, state_label)}</div>'
            f'<div class="title">{esc(b.get("title"))}</div>'
            f'<div class="when">{esc(cabin)} · {esc(fmt_date(start, tz))} · {esc(fmt_range(start, end, tz))}'
            f' · {esc(b.get("attendees"))} attendee(s)</div>{extra}</div>')


def timeline_chart(rules: Rules, cabins: list[dict], intervals: list[Interval], day: date,
                   now: datetime | None = None) -> None:
    """Gantt-style view of reserved and free slots for one day (office time zone)."""
    import altair as alt
    import pandas as pd

    tz = rules.tz
    lo, hi = local_datetime(day, rules.open_time, tz), local_datetime(day, rules.close_time, tz)
    if lo is None or hi is None or not rules.is_working_day(day):
        empty_state("No office hours on this day", "Pick a working day to see the timeline.")
        return
    rows = []

    def add(cabin_name, start, end, state, detail):
        s, e = max(start, lo), min(end, hi)
        if e <= s:
            return
        rows.append({"Cabin": cabin_name, "start": s.astimezone(tz).replace(tzinfo=None),
                     "end": e.astimezone(tz).replace(tzinfo=None), "Status": STATE_LABELS[state],
                     "Time": fmt_range(s, e, tz), "Details": detail})

    for c in cabins:
        cid = str(c["id"])
        if not c.get("is_active", True):
            add(c["name"], lo, hi, "inactive", "Cabin inactive")
            continue
        for i in intervals:
            if i.cabin_id != cid:
                continue
            if i.kind == "maintenance":
                add(c["name"], i.start, i.end, "maintenance", i.reason or "Maintenance")
            elif i.is_mine:
                add(c["name"], i.start, i.end, "mine", i.title or "Your booking")
            else:
                add(c["name"], i.start, i.end, "occupied", i.booker_name or "Reserved")
        for a, b in free_windows(rules, intervals, cid, day, now):
            add(c["name"], a, b, "available", "Free to book")

    if not rows:
        empty_state("Nothing to show", "All of today's office hours have passed.")
        return
    order = [c["name"] for c in cabins]
    states = list(PALETTE)
    scale = alt.Scale(domain=[STATE_LABELS[s] for s in states], range=[PALETTE[s][0] for s in states])
    df = pd.DataFrame(rows)
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadius=4, height=18)
        .encode(
            x=alt.X("start:T", title=f"{fmt_date(day)} ({rules.timezone})",
                    scale=alt.Scale(domain=[lo.astimezone(tz).replace(tzinfo=None).isoformat(),
                                            hi.astimezone(tz).replace(tzinfo=None).isoformat()]),
                    axis=alt.Axis(format="%H:%M", tickCount=12, grid=True)),
            x2="end:T",
            y=alt.Y("Cabin:N", sort=order, title=None, axis=alt.Axis(labelOverlap=False, labelLimit=160)),
            color=alt.Color("Status:N", scale=scale, legend=alt.Legend(orient="top", title=None)),
            opacity=alt.condition(alt.datum.Status == STATE_LABELS["available"], alt.value(0.35), alt.value(0.95)),
            tooltip=["Cabin", "Status", "Time", "Details"],
        )
        .properties(height=max(220, 34 * len(order)))
    )
    st.altair_chart(chart, width="stretch")
