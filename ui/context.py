"""Per-browser-session state and the request context handed to every view.

Streamlit runs entirely on the server. The Supabase client (and its JWT) lives
in this session's server-side ``st.session_state``; no token or key is written
to cookies, local storage or page HTML. Closing or reloading the tab ends the
session, and idle sessions are signed out after SESSION_IDLE_MINUTES.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field

import streamlit as st

from config import AppConfig
from core.auth import Access, AuthService, Limiters
from core.booking_service import BookingService, Rules
from core.database import Repository, create_user_client

log = logging.getLogger(__name__)

SETTINGS_TTL_SECONDS = 60


@st.cache_resource(show_spinner=False)
def shared_limiters() -> Limiters:
    """Process-wide, so limits apply across browser sessions."""
    return Limiters.default()


@st.cache_resource(show_spinner=False)
def shared_notifier(_cfg: AppConfig):
    from core.notifications import build_worker

    try:
        return build_worker(_cfg)
    except Exception:  # noqa: BLE001 - e-mail must never break the app
        log.exception("Could not initialise the notification worker")
        return None


def session_client(cfg: AppConfig):
    if "sb_client" not in st.session_state:
        st.session_state.sb_client = create_user_client(cfg.supabase_url, cfg.supabase_anon_key)
    return st.session_state.sb_client


def auth_service(cfg: AppConfig) -> AuthService:
    return AuthService(session_client(cfg), cfg.allowed_email_domains, shared_limiters(),
                       redirect_url=cfg.app_base_url)


def mark_signed_in() -> None:
    st.session_state.signed_in = True
    st.session_state.last_activity = _time.time()
    st.session_state.pop("settings_cache", None)


def sign_out(cfg: AppConfig, message: str | None = None) -> None:
    try:
        auth_service(cfg).sign_out()
    finally:
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        if message:
            st.session_state.flash = ("info", message)


def idle_expired(cfg: AppConfig) -> bool:
    last = st.session_state.get("last_activity")
    now = _time.time()
    st.session_state.last_activity = now
    return last is not None and now - last > cfg.session_idle_minutes * 60


@dataclass
class AppContext:
    cfg: AppConfig
    repo: Repository
    access: Access
    settings: dict
    rules: Rules
    notifier: object | None = None
    pages: dict = field(default_factory=dict)
    _service: BookingService | None = field(default=None, repr=False)

    @property
    def tz(self):
        return self.rules.tz

    @property
    def app_name(self) -> str:
        return self.settings.get("app_name") or self.cfg.app_name

    @property
    def company_name(self) -> str:
        return self.settings.get("company_name") or self.cfg.company_name

    @property
    def booking_service(self) -> BookingService:
        if self._service is None:
            self._service = BookingService(self.repo, self.rules, self.notifier)
        return self._service

    def invalidate_settings(self) -> None:
        st.session_state.pop("settings_cache", None)


def load_settings(repo: Repository, force: bool = False) -> dict:
    cached = st.session_state.get("settings_cache")
    if cached and not force and _time.time() - cached[0] < SETTINGS_TTL_SECONDS:
        return cached[1]
    settings = repo.get_settings()
    st.session_state.settings_cache = (_time.time(), settings)
    return settings
