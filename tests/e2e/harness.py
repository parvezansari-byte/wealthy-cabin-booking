"""Renders one view for a signed-in test user (used by the AppTest-based e2e tests)."""
import importlib
import os

import streamlit as st

from config import get_config
from core.booking_service import Rules
from core.database import Repository
from ui.context import AppContext, auth_service, load_settings, mark_signed_in
from ui.theme import inject_css

inject_css()
cfg = get_config()
auth = auth_service(cfg)
if not st.session_state.get("signed_in"):
    auth.sign_in(os.environ["WCB_E2E_EMAIL"], os.environ["WCB_E2E_PASSWORD"])
    mark_signed_in()
repo = Repository(st.session_state.sb_client)
access = auth.refresh_access()
settings = load_settings(repo)
ctx = AppContext(cfg=cfg, repo=repo, access=access, settings=settings, rules=Rules.from_settings(settings))
importlib.import_module(f"views.{os.environ['WCB_E2E_VIEW']}").render(ctx)
