"""Sign-in, registration (company e-mail only), e-mail verification and password reset."""
from __future__ import annotations

import streamlit as st

from config import AppConfig
from core.auth import AuthService
from core.errors import AppError
from core.security import esc
from ui.components import flash
from ui.context import mark_signed_in


def _brand(cfg: AppConfig, branding: dict) -> None:
    app = branding.get("app_name") or cfg.app_name
    company = branding.get("company_name") or cfg.company_name
    logo = branding.get("logo_url") or cfg.logo_url
    logo_html = (f'<img src="{esc(logo)}" alt="{esc(company)} logo" style="height:48px">'
                 if logo.startswith("https://") else '<div class="logo" aria-hidden="true">🏢</div>')
    st.markdown(f'<div class="wcb-auth-brand">{logo_html}<h1>{esc(app)}</h1>'
                f'<p>Book meeting cabins at {esc(company)} — sign in with your official '
                f'@{esc(cfg.allowed_email_domains[0])} e-mail.</p></div>', unsafe_allow_html=True)


def render(cfg: AppConfig, auth: AuthService, branding: dict) -> None:
    _, mid, _ = st.columns([1, 1.6, 1])
    with mid:
        _brand(cfg, branding)
        flash()
        domain = cfg.allowed_email_domains[0]
        t_in, t_up, t_verify, t_reset = st.tabs(["Sign in", "Create account", "Verify e-mail", "Forgot password"])

        with t_in:
            with st.form("signin", clear_on_submit=False):
                email = st.text_input("Work e-mail", placeholder=f"name@{domain}", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
            if submitted:
                try:
                    with st.spinner("Signing in…"):
                        auth.sign_in(email, password)
                except AppError as err:
                    st.error(err.message)
                else:
                    mark_signed_in()
                    st.rerun()

        with t_up:
            st.caption(f"Only verified **@{domain}** addresses can register. New accounts may need an "
                       "administrator's approval before they can book.")
            with st.form("signup", clear_on_submit=False):
                name = st.text_input("Full name", max_chars=120, autocomplete="name")
                email = st.text_input("Work e-mail", placeholder=f"name@{domain}", key="su_email",
                                      autocomplete="email")
                pw1 = st.text_input("Password", type="password", key="su_pw1", autocomplete="new-password",
                                    help="At least 10 characters with letters and numbers.")
                pw2 = st.text_input("Confirm password", type="password", key="su_pw2", autocomplete="new-password")
                submitted = st.form_submit_button("Create account", type="primary", width="stretch")
            if submitted:
                if pw1 != pw2:
                    st.error("The passwords do not match.")
                else:
                    try:
                        with st.spinner("Creating your account…"):
                            message = auth.sign_up(email, pw1, name)
                    except AppError as err:
                        st.error(err.message)
                    else:
                        # Pre-fill the verification tab (its widget is created later in this run).
                        st.session_state.vf_email = email.strip().lower()
                        st.success(message)

        with t_verify:
            st.caption("Enter the 6-digit code from your verification e-mail. "
                       "(If you clicked the link in the e-mail instead, just sign in.)")
            with st.form("verify"):
                email = st.text_input("Work e-mail", key="vf_email")
                code = st.text_input("Verification code", max_chars=10, key="vf_code",
                                     autocomplete="one-time-code")
                c1, c2 = st.columns(2)
                verify = c1.form_submit_button("Verify", type="primary", width="stretch")
                resend = c2.form_submit_button("Resend code", width="stretch")
            if verify:
                try:
                    auth.verify_email(email, code)
                except AppError as err:
                    st.error(err.message)
                else:
                    mark_signed_in()
                    st.rerun()
            elif resend:
                try:
                    st.info(auth.resend_verification(email))
                except AppError as err:
                    st.error(err.message)

        with t_reset:
            with st.form("reset_request"):
                email = st.text_input("Work e-mail", key="rs_email")
                send = st.form_submit_button("Send reset code", width="stretch")
            if send:
                try:
                    st.info(auth.request_password_reset(email))
                    st.session_state.rs_email2 = email.strip().lower()
                except AppError as err:
                    st.error(err.message)
            with st.form("reset_confirm"):
                email = st.text_input("Work e-mail", key="rs_email2")
                code = st.text_input("Reset code", max_chars=10, key="rs_code", autocomplete="one-time-code")
                pw1 = st.text_input("New password", type="password", key="rs_pw1", autocomplete="new-password")
                pw2 = st.text_input("Confirm new password", type="password", key="rs_pw2", autocomplete="new-password")
                done = st.form_submit_button("Set new password", type="primary", width="stretch")
            if done:
                if pw1 != pw2:
                    st.error("The passwords do not match.")
                else:
                    try:
                        auth.reset_password(email, code, pw1)
                    except AppError as err:
                        st.error(err.message)
                    else:
                        st.success("Your password has been changed. Please sign in.")

        st.caption("🔒 Your password is handled by Supabase Auth and is never stored by this app.")


def render_blocked(cfg: AppConfig, email: str, message: str, on_sign_out, on_retry) -> None:
    _, mid, _ = st.columns([1, 1.6, 1])
    with mid:
        st.markdown(f'<div class="wcb-auth-brand"><div class="logo" aria-hidden="true">⏳</div>'
                    f'<h1>Access not yet available</h1><p>{esc(email)}</p></div>', unsafe_allow_html=True)
        st.info(message)
        c1, c2 = st.columns(2)
        if c1.button("Check again", width="stretch"):
            on_retry()
        if c2.button("Sign out", type="primary", width="stretch"):
            on_sign_out()
