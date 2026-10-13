"""Profile details and password change for the signed-in employee."""
from __future__ import annotations

import streamlit as st

from core.errors import AppError
from core.security import clean_text
from ui.components import flash, page_header
from ui.context import AppContext, auth_service


def render(ctx: AppContext) -> None:
    a = ctx.access
    page_header("My account", "Your role and access are managed by administrators.")
    flash()
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.subheader("Profile")
        with st.form("profile"):
            st.text_input("Work e-mail", value=a.email, disabled=True)
            name = st.text_input("Full name", value=a.full_name, max_chars=120)
            dept = st.text_input("Department (optional)", value=a.department or "", max_chars=80)
            st.text_input("Role", value="Administrator" if a.is_admin else "Employee", disabled=True)
            if st.form_submit_button("Save profile", type="primary"):
                name = clean_text(name, 120)
                if len(name) < 2:
                    st.error("Please enter your full name.")
                else:
                    try:
                        ctx.repo.update_my_profile(a.user_id, name, clean_text(dept, 80) or None)
                    except AppError as err:
                        st.error(err.message)
                    else:
                        st.session_state.flash = ("success", "Profile saved.")
                        st.rerun()
    with c2:
        st.subheader("Change password")
        with st.form("password", clear_on_submit=True):
            pw1 = st.text_input("New password", type="password", autocomplete="new-password",
                                help="At least 10 characters with letters and numbers.")
            pw2 = st.text_input("Confirm new password", type="password", autocomplete="new-password")
            if st.form_submit_button("Update password"):
                if pw1 != pw2:
                    st.error("The passwords do not match.")
                else:
                    try:
                        auth_service(ctx.cfg).change_password(pw1)
                    except AppError as err:
                        st.error(err.message)
                    else:
                        st.success("Password updated.")
