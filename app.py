"""Wealthy Office Cabin Booking — Streamlit entry point.

Run locally:   streamlit run app.py
On Render:     streamlit run app.py --server.port $PORT --server.address 0.0.0.0
"""
from __future__ import annotations

import logging
import os

import streamlit as st

from config import ConfigError, get_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wealthy.app")

st.set_page_config(page_title="Wealthy Office Cabin Booking", page_icon="🏢", layout="wide",
                   initial_sidebar_state="auto", menu_items={"About": "Wealthy Office Cabin Booking"})

from core.auth import evaluate_access  # noqa: E402
from core.booking_service import Rules  # noqa: E402
from core.database import Repository  # noqa: E402
from core.errors import AppError  # noqa: E402
from core.security import esc  # noqa: E402
from ui.context import (AppContext, auth_service, idle_expired, load_settings, session_client,  # noqa: E402
                        shared_notifier, sign_out)
from ui.theme import inject_css  # noqa: E402
from views import account, admin_dashboard, cabins, employee_dashboard, login, my_bookings  # noqa: E402


def main() -> None:
    inject_css()
    try:
        cfg = get_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        st.error("The application is not configured correctly. An administrator has been notified in the "
                 "server logs.")
        st.stop()

    client = session_client(cfg)
    auth = auth_service(cfg)
    repo = Repository(client)

    def do_sign_out(message: str | None = None):
        sign_out(cfg, message)
        st.rerun()

    if not st.session_state.get("signed_in"):
        try:
            branding = repo.get_branding()
        except AppError:
            branding = {}
        login.render(cfg, auth, branding)
        return

    if idle_expired(cfg):
        do_sign_out("You were signed out after a period of inactivity.")

    # Re-check identity and authorisation on every run (cheap single RPC).
    try:
        access = auth.refresh_access()
    except AppError as err:
        do_sign_out(err.message if err.code != "UNEXPECTED" else "Please sign in again.")
        return
    ok, message = evaluate_access(access, cfg.allowed_email_domains)
    if not ok:
        login.render_blocked(cfg, access.email if access else "", message,
                             on_sign_out=lambda: do_sign_out(), on_retry=st.rerun)
        return

    try:
        settings = load_settings(repo)
    except AppError as err:
        st.error(err.message)
        st.stop()

    ctx = AppContext(cfg=cfg, repo=repo, access=access, settings=settings,
                     rules=Rules.from_settings(settings), notifier=shared_notifier(cfg))

    pages = {
        "dashboard": st.Page(lambda: employee_dashboard.render(ctx), title="Dashboard", icon="🏠",
                             url_path="dashboard", default=True),
        "cabins": st.Page(lambda: cabins.render(ctx), title="Availability", icon="🗓️", url_path="availability"),
        "my_bookings": st.Page(lambda: my_bookings.render(ctx), title="My bookings", icon="📋",
                               url_path="my-bookings"),
        "account": st.Page(lambda: account.render(ctx), title="My account", icon="👤", url_path="account"),
    }
    if access.is_admin:
        pages["admin"] = st.Page(lambda: admin_dashboard.render(ctx), title="Administration", icon="🛡️",
                                 url_path="admin")
    ctx.pages = pages

    sections = {"Booking": [pages["dashboard"], pages["cabins"], pages["my_bookings"]],
                "Account": [pages["account"]]}
    if access.is_admin:
        sections["Admin"] = [pages["admin"]]
    nav = st.navigation(sections)

    with st.sidebar:
        logo = ctx.settings.get("logo_url") or cfg.logo_url
        if logo and logo.startswith("https://"):
            st.image(logo, width=120)
        st.markdown(f"**{esc(ctx.app_name)}**  \n{esc(ctx.company_name)}")
        st.divider()
        st.markdown(f"{esc(access.display_name)}  \n<small>{esc(access.email)}</small>", unsafe_allow_html=True)
        if st.button("Sign out", width="stretch"):
            do_sign_out("You have been signed out.")

    nav.run()


main()
