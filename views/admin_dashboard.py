"""Administrator panel. Every action here is re-authorised by the database
(private.is_admin() / RLS); hiding this page from employees is only cosmetic."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import available_timezones

import pandas as pd
import streamlit as st

from core.errors import AppError
from core.reports import bookings_csv, rows_csv, utilization
from core.security import clean_text, csv_safe, is_company_email, normalize_email
from core.timeutil import day_bounds, fmt_date, fmt_range, local_datetime, now_utc, parse_ts
from ui.components import empty_state, flash, metric, page_header
from ui.context import AppContext
from ui.dialogs import open_cancel, render_open_dialogs
from views.shared import guarded, load_cabins

WEEKDAYS = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
EQUIPMENT_CHOICES = ["TV display", "Projector", "Video conferencing", "Whiteboard", "Speakerphone",
                     "HDMI cable", "Wireless screen sharing"]


def _done(message: str) -> None:
    st.session_state.flash = ("success", message)
    st.rerun()


def _act(fn, *args, success: str, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except AppError as err:
        st.error(err.message)
        return None
    _done(success if not callable(success) else success(result))
    return result


def _local_range(ctx: AppContext, start_day: date, end_day: date) -> tuple[datetime, datetime]:
    lo, _ = day_bounds(start_day, ctx.tz)
    hi, _ = day_bounds(end_day + timedelta(days=1), ctx.tz)
    return lo, hi


def _date_range(label: str, key: str, default: tuple[date, date]) -> tuple[date, date] | None:
    value = st.date_input(label, value=default, key=key, format="DD/MM/YYYY")
    if isinstance(value, (tuple, list)) and len(value) == 2:
        if (value[1] - value[0]).days > 366:
            st.warning("Please choose a range of one year or less.")
            return None
        return value[0], value[1]
    st.caption("Select a start and end date.")
    return None


# ---------------------------------------------------------------------------------- overview
def _overview(ctx: AppContext, cabins: list[dict]) -> None:
    tz, now = ctx.tz, now_utc()
    today = now.astimezone(tz).date()
    lo_today, hi_today = day_bounds(today, tz)
    today_rows = guarded(ctx.repo.admin_bookings, lo_today, hi_today)
    upcoming_rows = guarded(ctx.repo.admin_bookings, now, now + timedelta(days=60))
    confirmed_today = [b for b in today_rows if b["status"] == "confirmed"]
    occupied_now = {b["cabin_id"] for b in confirmed_today
                    if parse_ts(b["start_at"]) <= now < parse_ts(b["end_at"])}
    active = [c for c in cabins if c.get("is_active")]

    m = st.columns(5)
    with m[0]:
        metric("Total cabins", len(cabins), f"{len(active)} active")
    with m[1]:
        metric("Today's bookings", len(confirmed_today), fmt_date(today))
    with m[2]:
        metric("Occupied now", f"{len(occupied_now)}/{len(active)}", "cabins in a meeting now")
    with m[3]:
        metric("Upcoming", sum(1 for b in upcoming_rows if b["status"] == "confirmed"), "next 60 days")
    with m[4]:
        metric("Cancelled today", sum(1 for b in today_rows if b["status"] == "cancelled"), "scheduled for today")

    st.subheader("Utilisation report")
    rng = _date_range("Report period", "ov_range", (today - timedelta(days=29), today))
    if not rng:
        return
    lo, hi = _local_range(ctx, *rng)
    rows = guarded(ctx.repo.admin_bookings, lo, hi)
    report = utilization(ctx.rules, cabins, rows, *rng)
    df = pd.DataFrame(report)
    if df.empty:
        empty_state("No data for this period")
        return
    total_booked = int(df["booked_minutes"].sum())
    total_avail = int(df["available_minutes"].sum())
    st.caption(f"{len(rows)} bookings ({sum(1 for r in rows if r['status'] == 'cancelled')} cancelled) · "
               f"overall utilisation {100 * total_booked / total_avail if total_avail else 0:.1f}% of office hours")
    st.bar_chart(df.set_index("cabin")["utilization_pct"], y_label="Utilisation (%)", x_label=None,
                 color="#1E40AF", height=260)
    st.dataframe(df.rename(columns={"cabin": "Cabin", "code": "Code", "bookings": "Bookings",
                                    "booked_minutes": "Booked min", "available_minutes": "Office min",
                                    "cancelled": "Cancelled", "utilization_pct": "Utilisation %"}),
                 hide_index=True, width="stretch")
    st.download_button("⬇ Export utilisation CSV",
                       rows_csv(report, ["code", "cabin", "bookings", "cancelled", "booked_minutes",
                                         "available_minutes", "utilization_pct"]),
                       file_name=f"utilisation_{rng[0]}_{rng[1]}.csv", mime="text/csv")


# ---------------------------------------------------------------------------------- bookings
def _bookings(ctx: AppContext, cabins: list[dict]) -> None:
    tz, now = ctx.tz, now_utc()
    today = now.astimezone(tz).date()
    c1, c2, c3, c4 = st.columns([1.6, 1, 1, 1.4])
    with c1:
        rng = _date_range("Dates", "bk_admin_range", (today, today + timedelta(days=14)))
    status = c2.selectbox("Status", ["All", "confirmed", "cancelled"], key="bk_admin_status")
    cabin = c3.selectbox("Cabin", ["All", *[c["name"] for c in cabins]], key="bk_admin_cabin")
    query = c4.text_input("Search", placeholder="Reference, title, e-mail", key="bk_admin_q")
    if not rng:
        return
    rows = guarded(ctx.repo.admin_bookings, *_local_range(ctx, *rng))
    q = query.strip().lower()
    rows = [b for b in rows
            if (status == "All" or b["status"] == status)
            and (cabin == "All" or (b.get("cabins") or {}).get("name") == cabin)
            and (not q or q in " ".join([b.get("reference", ""), b.get("title", ""),
                                          (b.get("owner") or {}).get("email", "")]).lower())]
    if not rows:
        empty_state("No bookings match these filters")
        return
    table = [{
        "Reference": b["reference"], "Cabin": (b.get("cabins") or {}).get("name"),
        "Date": fmt_date(parse_ts(b["start_at"]), tz),
        "Time": fmt_range(parse_ts(b["start_at"]), parse_ts(b["end_at"]), tz),
        "Title": b["title"], "Attendees": b["attendees"],
        "Employee": (b.get("owner") or {}).get("full_name") or (b.get("owner") or {}).get("email"),
        "Status": b["status"], "Cancel reason": b.get("cancel_reason") or "",
    } for b in rows]
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")

    e1, e2 = st.columns([1, 1])
    include_notes = e2.checkbox("Include private meeting notes in export", value=False, key="bk_admin_notes")
    e1.download_button("⬇ Export bookings CSV", bookings_csv(rows, ctx.rules, include_notes=include_notes),
                       file_name=f"bookings_{rng[0]}_{rng[1]}.csv", mime="text/csv")

    cancellable = [b for b in rows if b["status"] == "confirmed" and parse_ts(b["end_at"]) > now]
    if cancellable:
        st.markdown("##### Cancel a booking")
        cc1, cc2 = st.columns([3, 1], vertical_alignment="bottom")
        choice = cc1.selectbox(
            "Booking", cancellable, key="bk_admin_pick",
            format_func=lambda b: f"{b['reference']} · {(b.get('cabins') or {}).get('name')} · "
                                  f"{fmt_date(parse_ts(b['start_at']), tz)} "
                                  f"{fmt_range(parse_ts(b['start_at']), parse_ts(b['end_at']), tz)} · {b['title']}")
        if cc2.button("Cancel booking…", width="stretch", key="bk_admin_cancel"):
            open_cancel(choice, as_admin=True)


# ---------------------------------------------------------------------------------- employees
def _employees(ctx: AppContext) -> None:
    profiles = guarded(ctx.repo.list_profiles)
    me = ctx.access.user_id
    pending = [p for p in profiles if p["status"] == "pending"]
    if pending:
        st.info(f"{len(pending)} account(s) are waiting for approval.")
    c1, c2 = st.columns([1, 2])
    status = c1.selectbox("Show", ["All", "pending", "active", "disabled"], key="emp_status")
    query = c2.text_input("Search", placeholder="Name or e-mail", key="emp_q").strip().lower()
    shown = [p for p in profiles if (status == "All" or p["status"] == status)
             and (not query or query in f"{p['email']} {p['full_name']}".lower())]
    st.dataframe(pd.DataFrame([{
        "E-mail": p["email"], "Name": p["full_name"], "Department": p.get("department") or "",
        "Role": p["role"], "Status": p["status"],
        "Joined": parse_ts(p["created_at"]).astimezone(ctx.tz).strftime("%d %b %Y"),
    } for p in shown]) if shown else pd.DataFrame(), hide_index=True, width="stretch")

    others = [p for p in shown if str(p["id"]) != me]
    st.markdown("##### Manage access")
    if not others:
        st.caption("No other employees in this view. (You cannot change your own access or role.)")
    else:
        target = st.selectbox("Employee", others, key="emp_pick",
                              format_func=lambda p: f"{p['email']} — {p['status']}, {p['role']}")
        b1, b2, b3 = st.columns(3)
        if target["status"] != "active":
            if b1.button("✅ Approve / enable access", type="primary", width="stretch"):
                _act(ctx.repo.set_employee_status, str(target["id"]), "active",
                     success=f"{target['email']} can now book cabins.")
        else:
            cancel_future = b1.checkbox("Also cancel their future bookings", key="emp_cancel_future")
            if b1.button("⛔ Disable access", width="stretch"):
                _act(ctx.repo.set_employee_status, str(target["id"]), "disabled", cancel_future,
                     success=lambda r: f"{target['email']} disabled. History kept; "
                                       f"{r.get('cancelled_bookings', 0)} future booking(s) cancelled.")
        if target["status"] == "active":
            if target["role"] == "admin":
                if b2.button("Remove administrator role", width="stretch"):
                    _act(ctx.repo.set_role, str(target["id"]), "employee",
                         success=f"{target['email']} is now an employee.")
            else:
                confirm = b3.checkbox("I confirm this person should be an administrator", key="emp_admin_ok")
                if b2.button("Make administrator", width="stretch", disabled=not confirm):
                    _act(ctx.repo.set_role, str(target["id"]), "admin",
                         success=f"{target['email']} is now an administrator.")

    st.markdown("##### Pre-approved addresses (allowlist)")
    policy = ctx.settings.get("access_policy", "approval")
    st.caption("Under the **approval** policy, a verified address on this list is activated automatically; "
               "other verified addresses wait for approval. "
               f"Current policy: **{policy}**. Only @{', @'.join(ctx.cfg.allowed_email_domains)} addresses.")
    allow = guarded(ctx.repo.list_allowlist)
    with st.form("allow_add", clear_on_submit=True):
        a1, a2, a3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        email = a1.text_input("E-mail to pre-approve")
        note = a2.text_input("Note (optional)", max_chars=200)
        if a3.form_submit_button("Add", width="stretch"):
            email = normalize_email(email)
            if not is_company_email(email, ctx.cfg.allowed_email_domains):
                st.error("Only company e-mail addresses can be added.")
            else:
                _act(ctx.repo.add_allowlist, email, clean_text(note, 200), success=f"{email} pre-approved.")
    if allow:
        r1, r2 = st.columns([3, 1], vertical_alignment="bottom")
        pick = r1.selectbox("Allowlisted addresses", [a["email"] for a in allow], key="allow_pick")
        if r2.button("Remove", width="stretch", key="allow_remove"):
            _act(ctx.repo.remove_allowlist, pick,
                 success=f"{pick} removed from the allowlist (existing access is unchanged).")
    else:
        st.caption("The allowlist is empty.")


# ---------------------------------------------------------------------------------- cabins
def _cabins(ctx: AppContext, cabins: list[dict]) -> None:
    st.caption("Edit cabin details without code changes. Deactivating a cabin blocks new bookings; "
               "existing bookings are kept (cancel them from the Bookings tab if needed).")
    for c in cabins:
        state = "Active" if c.get("is_active") else "Inactive"
        with st.expander(f"{c['code']} · {c['name']} · seats {c['capacity']} · {state}"):
            with st.form(f"cabin_{c['id']}"):
                f1, f2 = st.columns(2)
                name = f1.text_input("Name", value=c["name"], max_chars=60)
                location = f2.text_input("Location (optional)", value=c.get("location") or "", max_chars=120)
                f3, f4, f5 = st.columns(3)
                capacity = f3.number_input("Seating capacity", 1, 100, int(c["capacity"]))
                order = f4.number_input("Display order", 0, 1000, int(c.get("sort_order") or 0))
                active = f5.toggle("Active", value=bool(c.get("is_active")))
                current = list(c.get("equipment") or [])
                equipment = st.multiselect("Equipment", sorted(set(EQUIPMENT_CHOICES) | set(current)),
                                           default=current)
                extra = st.text_input("Other equipment (comma-separated)", max_chars=200)
                if st.form_submit_button("Save cabin", type="primary"):
                    items = equipment + [clean_text(x, 40) for x in extra.split(",") if x.strip()]
                    items = list(dict.fromkeys(i for i in items if i))[:12]
                    name = clean_text(name, 60)
                    if not name:
                        st.error("Name is required.")
                    else:
                        _act(ctx.repo.update_cabin, str(c["id"]),
                             {"name": name, "location": clean_text(location, 120) or None,
                              "capacity": int(capacity), "sort_order": int(order),
                              "is_active": bool(active), "equipment": items},
                             success=f"{name} saved.")


# ---------------------------------------------------------------------------------- maintenance
def _maintenance(ctx: AppContext, cabins: list[dict]) -> None:
    tz, now = ctx.tz, now_utc()
    today = now.astimezone(tz).date()
    st.markdown("##### Block a cabin")
    with st.form("maint_new"):
        m1, m2 = st.columns(2)
        cabin = m1.selectbox("Cabin", cabins, format_func=lambda c: c["name"])
        reason = m2.text_input("Reason", max_chars=200, placeholder="e.g. AC servicing")
        m3, m4, m5, m6 = st.columns(4)
        sd = m3.date_input("From date", value=today, min_value=today, format="DD/MM/YYYY")
        st_ = m4.time_input("From time", value=time(9, 0), step=900)
        ed = m5.date_input("To date", value=today, min_value=today, format="DD/MM/YYYY")
        et = m6.time_input("To time", value=time(18, 0), step=900)
        cancel_conflicts = st.checkbox("Cancel any overlapping bookings and notify their owners")
        if st.form_submit_button("Create maintenance block", type="primary"):
            start, end = local_datetime(sd, st_, tz), local_datetime(ed, et, tz)
            if start is None or end is None or end <= start:
                st.error("The end must be after the start.")
            else:
                _act(ctx.repo.create_maintenance, str(cabin["id"]), start, end, clean_text(reason, 200),
                     cancel_conflicts,
                     success=lambda r: f"Maintenance block created; {r.get('cancelled_bookings', 0)} "
                                       "booking(s) cancelled.")
    st.markdown("##### Current and upcoming blocks")
    blocks = guarded(ctx.repo.list_maintenance, now)
    if not blocks:
        empty_state("No maintenance scheduled")
    for b in blocks:
        s, e = parse_ts(b["start_at"]), parse_ts(b["end_at"])
        r1, r2 = st.columns([5, 1], vertical_alignment="center")
        r1.markdown(f"**{(b.get('cabins') or {}).get('name')}** · {fmt_date(s, tz)} {s.astimezone(tz):%H:%M} → "
                    f"{fmt_date(e, tz)} {e.astimezone(tz):%H:%M} · {b['reason']}")
        if r2.button("Remove", key=f"maint_rm_{b['id']}", width="stretch"):
            _act(ctx.repo.remove_maintenance, str(b["id"]), success="Maintenance block removed.")


# ---------------------------------------------------------------------------------- settings
def _settings(ctx: AppContext) -> None:
    s = ctx.settings
    rules = ctx.rules
    db_domains = s.get("allowed_email_domains") or []
    env_domains = list(ctx.cfg.allowed_email_domains)
    st.markdown(f"**Allowed e-mail domain(s):** {', '.join('@' + d for d in env_domains)} "
                "(from the `ALLOWED_EMAIL_DOMAIN` environment variable; read-only here).")
    if sorted(db_domains) != sorted(env_domains):
        st.warning(f"The database allows {', '.join(db_domains)} but the server is configured for "
                   f"{', '.join(env_domains)}. Only addresses allowed by both can sign in. "
                   "Update `office_settings.allowed_email_domains` from the SQL editor to match.")
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
    with st.form("settings"):
        st.markdown("##### Branding")
        b1, b2, b3 = st.columns(3)
        app_name = b1.text_input("Application name", value=s.get("app_name") or "", max_chars=80)
        company = b2.text_input("Company name", value=s.get("company_name") or "", max_chars=80)
        logo = b3.text_input("Logo URL (https, optional)", value=s.get("logo_url") or "", max_chars=500)

        st.markdown("##### Office hours")
        o1, o2, o3 = st.columns(3)
        tz_name = o1.selectbox("Office time zone", zones, index=zones.index(rules.timezone)
                               if rules.timezone in zones else 0)
        open_t = o2.time_input("Opens", value=rules.open_time, step=900)
        close_t = o3.time_input("Closes", value=rules.close_time, step=900)
        days = st.multiselect("Working days", list(WEEKDAYS), default=list(rules.working_days),
                              format_func=WEEKDAYS.get)

        st.markdown("##### Booking rules")
        r1, r2, r3, r4 = st.columns(4)
        min_m = r1.number_input("Minimum duration (min)", 5, 120, rules.min_minutes, step=5)
        max_m = r2.number_input("Maximum duration (min)", 15, 120, rules.max_minutes, step=5,
                                help="Hard limit: 120 minutes, enforced by the database.")
        slot = r3.selectbox("Time-slot step", [5, 10, 15, 30], index=[5, 10, 15, 30].index(rules.slot_minutes))
        horizon = r4.number_input("Book up to (days ahead)", 1, 365, rules.horizon_days)
        r5, r6, r7 = st.columns(3)
        cutoff = r5.number_input("Employees can cancel until (min before start)", 0, 1440,
                                 rules.cancel_cutoff_minutes, step=5)
        reminder = r6.number_input("Reminder e-mail (min before; 0 = off)", 0, 1440,
                                   int(s.get("reminder_minutes_before") or 0), step=5)
        show_names = r7.toggle("Show booker names to all employees", value=bool(s.get("show_booker_names")),
                               help="Meeting titles and notes always stay private.")

        st.markdown("##### Access policy")
        policy = st.radio(
            "When a verified company address signs up…", ["approval", "domain"],
            index=0 if s.get("access_policy", "approval") == "approval" else 1, horizontal=True,
            format_func=lambda p: {"approval": "Require admin approval or allowlist (recommended)",
                                   "domain": "Activate any verified company address"}[p])

        if st.form_submit_button("Save settings", type="primary"):
            problems = []
            if close_t <= open_t:
                problems.append("Closing time must be after opening time.")
            if min_m > max_m:
                problems.append("Minimum duration cannot exceed the maximum.")
            if not days:
                problems.append("Choose at least one working day.")
            if logo and not logo.startswith("https://"):
                problems.append("Logo URL must start with https://")
            if problems:
                for p in problems:
                    st.error(p)
            else:
                changes = {
                    "app_name": clean_text(app_name, 80), "company_name": clean_text(company, 80),
                    "logo_url": logo.strip() or None, "timezone": tz_name,
                    "open_time": open_t.strftime("%H:%M"), "close_time": close_t.strftime("%H:%M"),
                    "working_days": sorted(int(d) for d in days),
                    "min_booking_minutes": int(min_m), "max_booking_minutes": min(int(max_m), 120),
                    "slot_minutes": int(slot), "booking_horizon_days": int(horizon),
                    "cancel_cutoff_minutes": int(cutoff), "reminder_minutes_before": int(reminder),
                    "show_booker_names": bool(show_names), "access_policy": policy,
                }
                try:
                    ctx.repo.update_settings(changes)
                except AppError as err:
                    st.error(err.message)
                else:
                    ctx.invalidate_settings()
                    _done("Settings saved. They apply to new bookings immediately.")


# ---------------------------------------------------------------------------------- audit / notifications
def _audit(ctx: AppContext) -> None:
    prefixes = {"All": None, "Bookings": "booking", "Employees & roles": "profile",
                "Cabins": "cabins", "Maintenance": "maintenance_blocks", "Settings": "office_settings",
                "Allowlist": "employee_allowlist", "Notifications": "notifications", "Admin bootstrap": "admin"}
    c1, c2 = st.columns([1, 1])
    choice = c1.selectbox("Category", list(prefixes), key="audit_cat")
    limit = c2.selectbox("Show latest", [100, 250, 500, 1000], key="audit_limit")
    rows = guarded(ctx.repo.audit_logs, limit, prefixes[choice])
    if not rows:
        empty_state("No audit entries")
        return
    table = [{"When": parse_ts(r["occurred_at"]).astimezone(ctx.tz).strftime("%d %b %Y %H:%M:%S"),
              "Actor": r.get("actor_email") or "system", "Action": r["action"],
              "Entity": f"{r['entity_type']}:{r.get('entity_id') or ''}",
              "Details": str(r.get("details") or {})[:300]} for r in rows]
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
    st.download_button("⬇ Export audit log CSV", rows_csv(table, list(table[0])),
                       file_name="audit_log.csv", mime="text/csv")


def _notifications(ctx: AppContext) -> None:
    rows = guarded(ctx.repo.notifications, None, 500)
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("pending", "sending", "sent", "failed", "skipped")}
    cols = st.columns(5)
    for col, (k, v) in zip(cols, counts.items()):
        with col:
            metric(k.title(), v, "latest 500")
    if not ctx.cfg.can_send_email:
        st.warning("E-mail sending is not configured on this server (SMTP_* and SUPABASE_SERVICE_ROLE_KEY). "
                   "Notifications stay queued until it is.")
    b1, b2, _ = st.columns([1, 1, 2])
    if b1.button("Retry failed", width="stretch", disabled=counts["failed"] == 0):
        _act(ctx.repo.retry_failed_notifications, success=lambda n: f"{n} notification(s) queued for retry.")
    if b2.button("Send queued now", width="stretch", disabled=ctx.notifier is None):
        try:
            result = ctx.notifier.process(limit=50)
        except Exception:  # noqa: BLE001
            st.error("Could not process the queue right now. It will be retried automatically.")
        else:
            _done(f"Sent {result['sent']}, failed {result['failed']}, skipped {result['skipped']}.")
    if rows:
        st.dataframe(pd.DataFrame([{
            "Created": parse_ts(r["created_at"]).astimezone(ctx.tz).strftime("%d %b %H:%M"),
            "Booking": (r.get("bookings") or {}).get("reference"), "Type": r["kind"],
            "To": r["recipient_email"], "Status": r["status"], "Attempts": r["attempts"],
            "Last error": csv_safe(r.get("last_error") or ""),
        } for r in rows]), hide_index=True, width="stretch")
    else:
        empty_state("No notifications yet")


def render(ctx: AppContext) -> None:
    if not ctx.access.is_admin:  # cosmetic; the database refuses non-admins regardless
        st.error("Administrator access is required.")
        st.stop()
    page_header("Administration", f"{ctx.company_name} · {ctx.rules.timezone}")
    flash()
    render_open_dialogs(ctx)
    cabins = guarded(load_cabins, ctx)
    tabs = st.tabs(["Overview", "Bookings", "Employees", "Cabins", "Maintenance", "Settings",
                    "Audit log", "Notifications"])
    with tabs[0]:
        _overview(ctx, cabins)
    with tabs[1]:
        _bookings(ctx, cabins)
    with tabs[2]:
        _employees(ctx)
    with tabs[3]:
        _cabins(ctx, cabins)
    with tabs[4]:
        _maintenance(ctx, cabins)
    with tabs[5]:
        _settings(ctx)
    with tabs[6]:
        _audit(ctx)
    with tabs[7]:
        _notifications(ctx)

